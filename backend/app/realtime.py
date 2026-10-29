from __future__ import annotations

import io
import json
import math
import sys
import wave
from array import array
from dataclasses import dataclass

from fastapi import WebSocket
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from app.calls import utc_now
from app.errors import DomainError
from app.gnani import SpeechClient
from app.llm import StructuredLLM
from app.models import AuditEvent
from app.planner import MIN_CONFIDENCE, MIN_TRANSCRIPT_CONFIDENCE
from app.streaming_stt import StreamingSTTClient, StreamingSTTError
from app.voice import MAX_AUDIO_BYTES, VoiceTurnService


@dataclass(frozen=True)
class RealtimeConfig:
    sample_rate: int = 16_000
    vad_threshold: int = 500
    silence_ms: int = 700
    max_utterance_ms: int = 15_000
    min_speech_ms: int = 300


def build_realtime_config(settings) -> RealtimeConfig:
    return RealtimeConfig(
        sample_rate=settings.realtime_sample_rate,
        vad_threshold=settings.realtime_vad_threshold,
        silence_ms=settings.realtime_silence_ms,
        max_utterance_ms=settings.realtime_max_utterance_ms,
        min_speech_ms=settings.realtime_min_speech_ms,
    )


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def frame_rms(frame: bytes) -> float:
    usable = len(frame) - (len(frame) % 2)
    if usable < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(frame[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    energy = sum(value * value for value in samples) / len(samples)
    return math.sqrt(energy)


def frame_is_speech(frame: bytes, threshold: int) -> bool:
    return frame_rms(frame) >= threshold


class UtteranceBuffer:
    """Accumulates PCM16 frames and applies simple energy endpointing."""

    def __init__(self, config: RealtimeConfig):
        self._config = config
        self.pcm = bytearray()
        self.speech_ms = 0
        self.silence_ms = 0
        self._saw_speech = False

    def add(self, frame: bytes) -> bool:
        """Returns True when this frame is the first speech frame."""
        frame_ms = round(len(frame) / 2 / self._config.sample_rate * 1000)
        is_speech = frame_is_speech(frame, self._config.vad_threshold)
        started = False
        if is_speech:
            if not self._saw_speech:
                self._saw_speech = True
                started = True
            self.speech_ms += frame_ms
            self.silence_ms = 0
        elif self._saw_speech:
            self.silence_ms += frame_ms
        if self._saw_speech:
            self.pcm.extend(frame)
        return started

    @property
    def has_utterance(self) -> bool:
        return self._saw_speech and self.speech_ms >= self._config.min_speech_ms

    @property
    def complete(self) -> bool:
        return self._saw_speech and (
            self.silence_ms >= self._config.silence_ms
            or self.speech_ms + self.silence_ms >= self._config.max_utterance_ms
        )

    def reset(self) -> None:
        self.pcm.clear()
        self.speech_ms = 0
        self.silence_ms = 0
        self._saw_speech = False


async def handle_voice_socket(
    websocket: WebSocket,
    session_id: str,
    db: Session,
    llm: StructuredLLM,
    speech: SpeechClient,
    config: RealtimeConfig,
    *,
    streaming: StreamingSTTClient | None = None,
    max_audio_bytes: int = MAX_AUDIO_BYTES,
    min_confidence: float = MIN_CONFIDENCE,
    min_transcript_confidence: float = MIN_TRANSCRIPT_CONFIDENCE,
) -> None:
    service = VoiceTurnService(
        db,
        llm,
        speech,
        max_audio_bytes=max_audio_bytes,
        min_confidence=min_confidence,
        min_transcript_confidence=min_transcript_confidence,
    )
    calls = service.calls

    def audit(event_type: str, payload: dict | None = None) -> None:
        db.add(
            AuditEvent(
                session_id=session_id,
                event_type=event_type,
                payload_json=json.dumps(payload or {}, default=str),
                created_at=utc_now(),
            )
        )
        db.commit()

    try:
        call = calls.get_call(session_id)
    except DomainError as error:
        await websocket.send_json({"type": "error", "detail": error.detail})
        await websocket.close(code=4404)
        return
    await websocket.send_json(
        {"type": "ready", "session_id": session_id, "state": call.state.value}
    )
    audit("REALTIME_CONNECTED", {"state": call.state.value})

    buffer = UtteranceBuffer(config)
    listening = False
    speaking = False
    stream = streaming
    stream_ok = False

    async def connect_stream() -> bool:
        nonlocal stream_ok
        if stream is None:
            return False
        try:
            await stream.connect()
        except StreamingSTTError as error:
            audit("STREAMING_STT_UNAVAILABLE", {"detail": str(error)})
            return False
        stream_ok = True
        return True

    async def forward(frame: bytes) -> None:
        nonlocal stream_ok
        if not stream_ok:
            return
        try:
            await stream.send_audio(frame)
        except StreamingSTTError as error:
            stream_ok = False
            audit("STREAMING_STT_FAILED", {"detail": str(error)})

    async def new_utterance() -> None:
        if stream_ok:
            await stream.begin_utterance()

    async def cancel_utterance() -> None:
        nonlocal stream_ok
        if not stream_ok:
            return
        try:
            await stream.cancel_utterance()
        except StreamingSTTError as error:
            stream_ok = False
            audit("STREAMING_STT_FAILED", {"detail": str(error)})

    async def send_turn(response) -> None:
        nonlocal speaking
        await websocket.send_json(
            {
                "type": "turn",
                "state": response.state,
                "transcript": response.transcript,
                "detected_language": response.detected_language,
                "transcript_confidence": response.transcript_confidence,
                "agent_message": response.agent_message,
                "turn": response.turn.model_dump(mode="json"),
                "planner": response.planner.model_dump(mode="json"),
                "timings": response.timings.model_dump(mode="json"),
                "audio_error": response.audio_error,
            }
        )
        if response.audio_base64:
            speaking = True
            await websocket.send_json(
                {
                    "type": "agent_audio",
                    "audio_base64": response.audio_base64,
                    "content_type": response.audio_content_type,
                }
            )

    async def process_utterance() -> None:
        nonlocal stream_ok
        await websocket.send_json(
            {"type": "speech_end", "duration_ms": buffer.speech_ms}
        )
        pcm = bytes(buffer.pcm)
        buffer.reset()
        if stream_ok:
            try:
                transcript = await stream.finish_utterance()
            except StreamingSTTError as error:
                stream_ok = False
                audit("STREAMING_STT_FAILED", {"detail": str(error)})
                transcript = None
            if transcript is not None:
                try:
                    response = await service.handle_transcript(
                        session_id,
                        transcript.text,
                        detected_language=transcript.language,
                        transcript_confidence=None,
                        stt_ms=transcript.latency_ms,
                        stt_mode="streaming",
                    )
                except DomainError as error:
                    await websocket.send_json({"type": "error", "detail": error.detail})
                    return
                await send_turn(response)
                return
        try:
            wav = pcm16_to_wav(pcm, config.sample_rate)
            response = await service.handle_audio(
                session_id, wav, "utterance.wav", "audio/wav"
            )
        except DomainError as error:
            await websocket.send_json({"type": "error", "detail": error.detail})
            return
        await send_turn(response)

    await connect_stream()

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                audit("REALTIME_DISCONNECTED", {"code": message.get("code")})
                break
            if message.get("bytes") is not None:
                frame = message["bytes"]
                if speaking:
                    if frame_is_speech(frame, config.vad_threshold):
                        speaking = False
                        listening = True
                        buffer.reset()
                        audit("BARGE_IN", {"rms": round(frame_rms(frame))})
                        await websocket.send_json({"type": "stop_speaking"})
                        await websocket.send_json({"type": "listening"})
                        await new_utterance()
                        await forward(frame)
                        if buffer.add(frame):
                            await websocket.send_json({"type": "speech_start"})
                    continue
                if not listening:
                    continue
                await forward(frame)
                if buffer.add(frame):
                    await websocket.send_json({"type": "speech_start"})
                if buffer.has_utterance and buffer.complete:
                    listening = False
                    await process_utterance()
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "detail": "Invalid control message"}
                )
                continue
            kind = control.get("type")
            if kind == "start":
                if stream is not None and not stream_ok:
                    await connect_stream()
                # A new turn starts after the previous reply finished playing;
                # only raw frames arriving mid-playback are a barge-in.
                speaking = False
                listening = True
                buffer.reset()
                await new_utterance()
                await websocket.send_json({"type": "listening"})
            elif kind == "stop":
                if listening and buffer.has_utterance:
                    listening = False
                    await process_utterance()
                elif listening:
                    listening = False
                    buffer.reset()
                    await cancel_utterance()
            elif kind == "reset":
                speaking = False
                listening = False
                buffer.reset()
                await cancel_utterance()
            else:
                await websocket.send_json(
                    {"type": "error", "detail": "Unknown control message"}
                )
    except WebSocketDisconnect:
        audit("REALTIME_DISCONNECTED", {"code": "client"})
    finally:
        if stream is not None:
            try:
                await stream.close()
            except StreamingSTTError:
                pass
