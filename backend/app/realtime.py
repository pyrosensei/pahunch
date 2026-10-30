from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from time import perf_counter

from fastapi import WebSocket
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from app.calls import utc_now
from app.errors import DomainError
from app.gnani import GnaniError, SpeechClient
from app.llm import StructuredLLM
from app.models import AuditEvent
from app.planner import MIN_CONFIDENCE, MIN_TRANSCRIPT_CONFIDENCE
from app.streaming_stt import StreamingSTTClient, StreamingSTTError
from app.voice import ACK_PHRASE, MAX_AUDIO_BYTES, VoiceTurnService

MIN_TTS_SAMPLE_RATE = 8_000
MAX_TTS_SAMPLE_RATE = 48_000

# An outbound call opens with the agent identifying itself; the customer should
# never have to speak into a silent line to find out who called.
OPENING_LINE = (
    "Namaste, main SwiftCart ki delivery team se baat kar rahi hoon. "
    "Aapke parcel ke baare mein ek minute baat karni hai — boliye?"
)


@dataclass(frozen=True)
class RealtimeConfig:
    sample_rate: int = 16_000
    vad_threshold: int = 500
    silence_ms: int = 600
    quiet_ms: int = 600
    max_utterance_ms: int = 15_000
    min_speech_ms: int = 300


def build_realtime_config(settings) -> RealtimeConfig:
    return RealtimeConfig(
        sample_rate=settings.realtime_sample_rate,
        vad_threshold=settings.realtime_vad_threshold,
        silence_ms=settings.realtime_silence_ms,
        quiet_ms=settings.realtime_quiet_ms,
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
    """Accumulates PCM16 frames and applies energy endpointing.

    Two thresholds end an utterance: a hard silence window for frames that
    have fallen to the noise floor, and a shorter quiet window for frames
    that are merely much quieter than speech. The quiet window is what keeps
    a natural pause from adding a full second of dead air to every turn.
    """

    QUIET_FLOOR_RATIO = 0.35

    def __init__(self, config: RealtimeConfig):
        self._config = config
        self.pcm = bytearray()
        self.speech_ms = 0
        self.silence_ms = 0
        self.quiet_ms = 0
        self._saw_speech = False

    def add(self, frame: bytes) -> bool:
        """Returns True when this frame is the first speech frame."""
        frame_ms = round(len(frame) / 2 / self._config.sample_rate * 1000)
        rms = frame_rms(frame)
        is_speech = rms >= self._config.vad_threshold
        started = False
        if is_speech:
            if not self._saw_speech:
                self._saw_speech = True
                started = True
            self.speech_ms += frame_ms
            self.silence_ms = 0
            self.quiet_ms = 0
        elif self._saw_speech:
            self.silence_ms += frame_ms
            if rms >= self._config.vad_threshold * self.QUIET_FLOOR_RATIO:
                self.quiet_ms += frame_ms
            else:
                self.quiet_ms = 0
        if self._saw_speech:
            self.pcm.extend(frame)
        return started

    @property
    def has_utterance(self) -> bool:
        return self._saw_speech and self.speech_ms >= self._config.min_speech_ms

    @property
    def complete(self) -> bool:
        if not self._saw_speech:
            return False
        if self.quiet_ms >= self._config.quiet_ms:
            return True
        return self.silence_ms >= self._config.silence_ms or (
            self.speech_ms + self.silence_ms >= self._config.max_utterance_ms
        )

    def reset(self) -> None:
        self.pcm.clear()
        self.speech_ms = 0
        self.silence_ms = 0
        self.quiet_ms = 0
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
    # The browser plays the agent at its own native AudioContext rate, so the
    # server asks Timbre for exactly that rate and ships raw PCM16.
    tts_sample_rate = config.sample_rate
    ack_tasks: dict[int, asyncio.Task] = {}
    greeting_task: asyncio.Task | None = None
    pending_listening = False

    async def prepare_ack() -> None:
        """Synthesise the acknowledgement up front so it can be played instantly."""
        rate = tts_sample_rate
        if rate in ack_tasks:
            return
        synthesize_pcm = getattr(speech, "synthesize_pcm", None)
        if synthesize_pcm is None:
            return

        async def run() -> None:
            try:
                pcm = await synthesize_pcm(ACK_PHRASE, sample_rate=rate)
            except GnaniError as error:
                audit("ACK_UNAVAILABLE", {"detail": str(error)})
                return
            service.set_ack(pcm, rate)

        ack_tasks[rate] = asyncio.create_task(run())

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
        """Announce the agent's reply before any audio exists."""
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
                "audio_streamed": response.audio_streamed,
            }
        )

    async def send_chunk(chunk: bytes) -> None:
        nonlocal speaking
        speaking = True
        await websocket.send_json(
            {
                "type": "agent_audio_chunk",
                "chunk_base64": base64.b64encode(chunk).decode("ascii"),
                "sample_rate": tts_sample_rate,
            }
        )

    async def send_audio_end(response) -> None:
        await websocket.send_json(
            {
                "type": "agent_audio_end",
                "audio_streamed": response.audio_streamed,
                "audio_error": response.audio_error,
                "ack_played": response.ack_played,
                "timings": response.timings.model_dump(mode="json"),
            }
        )

    async def greet() -> None:
        """Speak the opening line, then hand the line to the customer."""
        nonlocal speaking, listening, pending_listening
        started = perf_counter()
        first_audio_ms = 0
        byte_count = 0
        error: str | None = None
        stream_pcm = getattr(speech, "stream_pcm", None)
        try:
            if stream_pcm is None:
                error = "TTS_UNAVAILABLE"
            else:
                async for chunk in stream_pcm(OPENING_LINE, sample_rate=tts_sample_rate):
                    if not chunk:
                        continue
                    if not first_audio_ms:
                        first_audio_ms = round((perf_counter() - started) * 1000)
                    byte_count += len(chunk)
                    await send_chunk(chunk)
        except GnaniError:
            error = "TTS_UNAVAILABLE" if not byte_count else "TTS_INTERRUPTED"
        except RuntimeError:
            return  # socket closed mid-greeting
        speaking = False
        try:
            await websocket.send_json(
                {
                    "type": "agent_audio_end",
                    "audio_streamed": first_audio_ms > 0,
                    "audio_error": error,
                    "ack_played": False,
                    "timings": {
                        "first_audio_ms": first_audio_ms,
                        "tts_ms": round((perf_counter() - started) * 1000),
                        "total_ms": round((perf_counter() - started) * 1000),
                    },
                }
            )
        except RuntimeError:
            return
        audit(
            "OPENING_LINE_SPOKEN",
            {"bytes": byte_count, "first_audio_ms": first_audio_ms, "error": error},
        )
        if pending_listening:
            pending_listening = False
            listening = True
            buffer.reset()
            await new_utterance()
            await websocket.send_json({"type": "listening"})

    async def deliver_turn(
        text: str,
        *,
        detected_language: str | None,
        transcript_confidence: float | None,
        stt_ms: int,
        stt_mode: str,
        turn_started: float,
    ) -> None:
        nonlocal speaking
        try:
            response = await service.handle_transcript(
                session_id,
                text,
                detected_language=detected_language,
                transcript_confidence=transcript_confidence,
                stt_ms=stt_ms,
                stt_mode=stt_mode,
                turn_started=turn_started,
                turn_sink=send_turn,
                audio_sink=send_chunk,
                sample_rate=tts_sample_rate,
            )
        except DomainError as error:
            await websocket.send_json({"type": "error", "detail": error.detail})
            return
        if response.audio_base64:
            speaking = True
            await websocket.send_json(
                {
                    "type": "agent_audio",
                    "audio_base64": response.audio_base64,
                    "content_type": response.audio_content_type,
                }
            )
        await send_audio_end(response)

    async def process_utterance() -> None:
        nonlocal stream_ok, speaking
        await websocket.send_json(
            {"type": "speech_end", "duration_ms": buffer.speech_ms}
        )
        utterance_ended_at = perf_counter()
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
                await deliver_turn(
                    transcript.text,
                    detected_language=transcript.language,
                    transcript_confidence=None,
                    stt_ms=transcript.latency_ms,
                    stt_mode="streaming",
                    turn_started=utterance_ended_at,
                )
                return
        try:
            wav = pcm16_to_wav(pcm, config.sample_rate)
            response = await service.handle_audio(
                session_id,
                wav,
                "utterance.wav",
                "audio/wav",
                turn_sink=send_turn,
                audio_sink=send_chunk,
                sample_rate=tts_sample_rate,
            )
        except DomainError as error:
            await websocket.send_json({"type": "error", "detail": error.detail})
            return
        if response.audio_base64:
            speaking = True
            await websocket.send_json(
                {
                    "type": "agent_audio",
                    "audio_base64": response.audio_base64,
                    "content_type": response.audio_content_type,
                }
            )
        await send_audio_end(response)

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
                        if greeting_task is not None and not greeting_task.done():
                            greeting_task.cancel()
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
            if kind == "config":
                requested = control.get("tts_sample_rate")
                if (
                    isinstance(requested, int)
                    and MIN_TTS_SAMPLE_RATE <= requested <= MAX_TTS_SAMPLE_RATE
                ):
                    tts_sample_rate = requested
                await websocket.send_json(
                    {"type": "config_ack", "tts_sample_rate": tts_sample_rate}
                )
                if greeting_task is None:
                    greeting_task = asyncio.create_task(greet())
                await prepare_ack()
            elif kind == "start":
                if stream is not None and not stream_ok:
                    await connect_stream()
                # A new turn starts after the previous reply finished playing;
                # only raw frames arriving mid-playback are a barge-in.
                speaking = False
                buffer.reset()
                await new_utterance()
                if greeting_task is not None and not greeting_task.done():
                    # Hold the microphone until the opening line has played, so
                    # the agent never hears itself through the speakers.
                    pending_listening = True
                else:
                    listening = True
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
            elif kind == "scripted_turn":
                # A scripted turn bypasses speech recognition so the demo can
                # be replayed without a microphone. Everything downstream
                # (planner, policy, commit) is the live path.
                listening = False
                text = control.get("text")
                if not isinstance(text, str) or not text.strip():
                    await websocket.send_json(
                        {"type": "error", "detail": "scripted_turn needs a text field"}
                    )
                    continue
                await websocket.send_json(
                    {"type": "speech_end", "duration_ms": 0, "scripted": True}
                )
                await deliver_turn(
                    text.strip(),
                    detected_language=control.get("detected_language"),
                    transcript_confidence=None,
                    stt_ms=0,
                    stt_mode="scripted",
                    turn_started=perf_counter(),
                )
            else:
                await websocket.send_json(
                    {"type": "error", "detail": "Unknown control message"}
                )
    except WebSocketDisconnect:
        audit("REALTIME_DISCONNECTED", {"code": "client"})
    finally:
        for task in ack_tasks.values():
            task.cancel()
        if greeting_task is not None:
            greeting_task.cancel()
        if stream is not None:
            try:
                await stream.close()
            except StreamingSTTError:
                pass
