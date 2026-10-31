from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import sys
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from functools import partial
from time import perf_counter

from anyio import CancelScope
from fastapi import WebSocket
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from app.calls import CallService, utc_now
from app.errors import DomainError
from app.gnani import GnaniError, SpeechClient
from app.language import phrase
from app.llm import StructuredLLM
from app.models import AuditEvent
from app.planner import MIN_CONFIDENCE, MIN_TRANSCRIPT_CONFIDENCE
from app.streaming_stt import StreamingSTTClient, StreamingSTTError
from app.voice import MAX_AUDIO_BYTES, VoiceTurnService

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
    adaptive_vad: bool = True
    vad_floor: int = 120
    vad_ceiling: int = 12_000
    noise_ratio: float = 3.0
    calibration_ms: int = 300
    speech_start_ms: int = 100
    barge_in_ms: int = 160
    pre_roll_ms: int = 200
    no_speech_ms: int = 5_000

    def __post_init__(self):
        if not 0 < self.vad_floor < self.vad_ceiling <= 32767 or self.noise_ratio <= 1:
            raise ValueError("Invalid VAD threshold bounds or noise ratio")
        if (
            min(
                self.sample_rate,
                self.vad_threshold,
                self.calibration_ms,
                self.speech_start_ms,
                self.barge_in_ms,
                self.no_speech_ms,
                self.silence_ms,
                self.quiet_ms,
                self.min_speech_ms,
            )
            <= 0
        ):
            raise ValueError("Audio rates, thresholds and timing must be positive")
        if self.pre_roll_ms < 0 or self.max_utterance_ms < self.pre_roll_ms + max(
            self.speech_start_ms, self.barge_in_ms
        ):
            raise ValueError("Utterance cap must hold pre-roll and speech onset")


def build_realtime_config(settings) -> RealtimeConfig:
    return RealtimeConfig(
        sample_rate=settings.realtime_sample_rate,
        vad_threshold=settings.realtime_vad_threshold,
        silence_ms=settings.realtime_silence_ms,
        quiet_ms=settings.realtime_quiet_ms,
        max_utterance_ms=settings.realtime_max_utterance_ms,
        min_speech_ms=settings.realtime_min_speech_ms,
        adaptive_vad=settings.realtime_adaptive_vad,
        vad_floor=settings.realtime_vad_floor,
        vad_ceiling=settings.realtime_vad_ceiling,
        noise_ratio=settings.realtime_noise_ratio,
        calibration_ms=settings.realtime_calibration_ms,
        speech_start_ms=settings.realtime_speech_start_ms,
        barge_in_ms=settings.realtime_barge_in_ms,
        pre_roll_ms=settings.realtime_pre_roll_ms,
        no_speech_ms=settings.realtime_no_speech_ms,
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
    """Retain onset audio while calibrating only on idle listening frames."""

    QUIET_FLOOR_RATIO = 0.35

    def __init__(self, config: RealtimeConfig):
        self._config = config
        self._noise_levels: deque[tuple[float, int]] = deque(maxlen=50)
        self._calibration_samples = 0
        self.noise_floor = config.vad_threshold / config.noise_ratio
        self.rms = 0.0
        self.pcm = bytearray()
        self._lead = bytearray()
        self.reset()

    @property
    def threshold(self) -> float:
        if not self._config.adaptive_vad:
            return self._config.vad_threshold
        return max(
            self._config.vad_floor,
            min(self._config.vad_ceiling, self.noise_floor * self._config.noise_ratio),
        )

    @property
    def calibrating(self) -> bool:
        return self._config.adaptive_vad and self._calibration_samples * 1000 < (
            self._config.calibration_ms * self._config.sample_rate
        )

    @property
    def noise_limited(self) -> bool:
        return self._config.adaptive_vad and (
            self.noise_floor * self._config.noise_ratio >= self._config.vad_ceiling
        )

    @property
    def pre_roll(self) -> bytes:
        return bytes(self._lead)

    @property
    def speech_ms(self) -> int:
        return round(self._speech_samples * 1000 / self._config.sample_rate)

    @property
    def silence_ms(self) -> int:
        return round(self._silence_samples * 1000 / self._config.sample_rate)

    @property
    def quiet_ms(self) -> int:
        return round(self._quiet_samples * 1000 / self._config.sample_rate)

    def _observe_noise(self, samples: int) -> None:
        if not self._config.adaptive_vad:
            return
        self._noise_levels.append((self.rms, samples))
        self._calibration_samples += samples
        target = sum(count for _, count in self._noise_levels) * 0.2
        observed = 0
        for level, count in sorted(self._noise_levels):
            observed += count
            if observed >= target:
                self.noise_floor = level
                break

    def add(self, frame: bytes, *, calibrate: bool = True, start_ms: int | None = None) -> bool:
        usable = frame[: len(frame) - len(frame) % 2]
        samples = len(usable) // 2
        self.rejected_ms = 0
        if not samples or self.complete:
            return False
        self.rms = frame_rms(usable)
        if not self._saw_speech:
            limit_ms = self._config.pre_roll_ms + max(
                self._config.speech_start_ms, self._config.barge_in_ms
            )
            self._lead.extend(usable)
            del self._lead[
                : max(0, len(self._lead) - limit_ms * self._config.sample_rate * 2 // 1000)
            ]
            if calibrate and self.calibrating:
                self._observe_noise(samples)
                return False
            if self.rms < self.threshold or self.noise_limited:
                self.rejected_ms = round(self._candidate_samples * 1000 / self._config.sample_rate)
                self._candidate_samples = 0
                if calibrate:
                    self._observe_noise(samples)
                return False
            self._candidate_samples += samples
            required = self._config.speech_start_ms if start_ms is None else start_ms
            if self._candidate_samples * 1000 < required * self._config.sample_rate:
                return False
            self._saw_speech = True
            self._speech_threshold = self.threshold
            self._speech_samples = self._candidate_samples
            keep = (
                self._config.pre_roll_ms * self._config.sample_rate // 1000
                + self._candidate_samples
            )
            self.pcm.extend(self._lead[-keep * 2 :])
            self._lead.clear()
            del self.pcm[self._config.max_utterance_ms * self._config.sample_rate * 2 // 1000 :]
            return True
        remaining = self._config.max_utterance_ms * self._config.sample_rate * 2 // 1000 - len(
            self.pcm
        )
        usable = usable[:remaining]
        samples = len(usable) // 2
        if self.rms >= self._speech_threshold:
            self._speech_samples += samples
            self._silence_samples = self._quiet_samples = 0
        else:
            self._silence_samples += samples
            if self.rms >= self._speech_threshold * self.QUIET_FLOOR_RATIO:
                self._quiet_samples += samples
            else:
                self._quiet_samples = 0
        self.pcm.extend(usable)
        return False

    @property
    def has_utterance(self) -> bool:
        return self._saw_speech and self.speech_ms >= self._config.min_speech_ms

    @property
    def complete(self) -> bool:
        return self.end_reason is not None

    @property
    def end_reason(self) -> str | None:
        if not self._saw_speech:
            return None
        if len(self.pcm) * 1000 >= self._config.max_utterance_ms * self._config.sample_rate * 2:
            return "MAX_DURATION"
        if self.quiet_ms >= self._config.quiet_ms or self.silence_ms >= self._config.silence_ms:
            return "SILENCE"
        return None

    def reset(self) -> None:
        self.pcm.clear()
        self._lead.clear()
        self.rms = 0.0
        self._speech_samples = self._silence_samples = self._quiet_samples = 0
        self._candidate_samples = 0
        self.rejected_ms = 0
        self._saw_speech = False


# Bound pending STT audio by both bytes and frame count, independently of VAD.
_STT_QUEUE_FRAMES = 50
_STT_IO_TIMEOUT = 3.0


@dataclass
class _Capture:
    queue: asyncio.Queue[bytes | None]
    task: asyncio.Task | None = None
    queued_bytes: int = 0
    fallback_reason: str | None = None


class _StreamingInput:
    def __init__(self, stream, sample_rate, audit):
        self.stream = stream
        self.byte_limit = sample_rate * 2
        self.audit = audit
        self.connected = False
        self.capture: _Capture | None = None
        self.barrier: asyncio.Task | None = None
        self.connection = asyncio.create_task(self._connect()) if stream is not None else None

    async def _connect(self):
        try:
            await asyncio.wait_for(self.stream.connect(), 10)
        except (StreamingSTTError, TimeoutError, OSError, RuntimeError) as error:
            self.audit(
                "STREAMING_STT_UNAVAILABLE",
                {
                    "phase": "connect",
                    "error_type": type(error).__name__,
                },
            )
            return False
        self.connected = True
        return True

    def begin(self):
        capture = _Capture(asyncio.Queue(maxsize=_STT_QUEUE_FRAMES + 1))
        previous = self.barrier
        self.capture = capture
        if self.stream is not None:
            capture.task = asyncio.create_task(self._run(capture, previous))
        return capture

    async def _run(self, capture, previous):
        phase = "connect"
        try:
            if previous is not None:
                await asyncio.shield(previous)
            if self.connection is not None:
                await asyncio.shield(self.connection)
                self.connection = None
            if not self.connected and not await self._connect():
                capture.fallback_reason = "CONNECT_FAILED"
                return None
            phase = "begin"
            await asyncio.wait_for(self.stream.begin_utterance(), _STT_IO_TIMEOUT)
            while True:
                frame = await capture.queue.get()
                if frame is None:
                    phase = "finish"
                    return await asyncio.wait_for(self.stream.finish_utterance(), _STT_IO_TIMEOUT)
                capture.queued_bytes -= len(frame)
                phase = "send"
                await asyncio.wait_for(self.stream.send_audio(frame), _STT_IO_TIMEOUT)
        except (StreamingSTTError, TimeoutError, OSError, RuntimeError) as error:
            capture.fallback_reason = "STREAM_FAILED"
            self.connected = False
            self.audit(
                "STREAMING_STT_FAILED",
                {
                    "phase": phase,
                    "error_type": type(error).__name__,
                },
            )
            return None

    def offer(self, frame):
        capture = self.capture
        if capture is None or capture.task is None or capture.fallback_reason:
            return
        if (
            capture.queued_bytes + len(frame) > self.byte_limit
            or capture.queue.qsize() >= _STT_QUEUE_FRAMES
        ):
            capture.fallback_reason = "QUEUE_OVERFLOW"
            self.audit(
                "STREAMING_STT_FAILED",
                {
                    "phase": "queue",
                    "reason": capture.fallback_reason,
                    "queued_bytes": capture.queued_bytes,
                },
            )
            capture.task.cancel()
            return
        capture.queued_bytes += len(frame)
        capture.queue.put_nowait(frame)

    async def finish(self, capture):
        if capture is None or capture.task is None:
            return None
        if not capture.fallback_reason:
            capture.queue.put_nowait(None)
        try:
            result = await capture.task
        except asyncio.CancelledError:
            if capture.fallback_reason != "QUEUE_OVERFLOW":
                raise
            result = None
        if capture.fallback_reason:
            await self._discard()
        return result

    async def _discard(self):
        try:
            await asyncio.wait_for(self.stream.cancel_utterance(), _STT_IO_TIMEOUT)
        except (StreamingSTTError, TimeoutError, OSError, RuntimeError) as error:
            self.audit(
                "STREAMING_STT_FAILED",
                {
                    "phase": "cancel",
                    "error_type": type(error).__name__,
                },
            )
        self.connected = False

    def abort(self):
        capture, self.capture = self.capture, None
        previous = self.barrier
        if capture is not None and capture.task is not None:
            capture.task.cancel()

        async def cleanup():
            if previous is not None:
                await previous
            if capture is not None and capture.task is not None:
                await asyncio.gather(capture.task, return_exceptions=True)
                await self._discard()

        self.barrier = asyncio.create_task(cleanup())

    async def close(self):
        self.abort()
        if self.connection is not None:
            self.connection.cancel()
            await asyncio.gather(self.connection, return_exceptions=True)
        await self.barrier
        if self.stream is not None:
            try:
                await asyncio.wait_for(self.stream.close(), _STT_IO_TIMEOUT)
            except (StreamingSTTError, TimeoutError, OSError, RuntimeError) as error:
                self.audit(
                    "STREAMING_STT_FAILED",
                    {
                        "phase": "close",
                        "error_type": type(error).__name__,
                    },
                )


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
    bind = db.get_bind()

    def audit(event_type: str, payload: dict | None = None) -> None:
        with Session(bind) as audit_db:
            audit_db.add(
                AuditEvent(
                    session_id=session_id,
                    event_type=event_type,
                    payload_json=json.dumps(payload or {}, default=str),
                    created_at=utc_now(),
                )
            )
            audit_db.commit()

    try:
        call = CallService(db).get_call(session_id)
    except DomainError as error:
        await websocket.send_json({"type": "error", "detail": error.detail})
        await websocket.close(code=4404)
        return
    state = call.state.value
    # Release the dependency session's read transaction before background turns.
    db.rollback()
    await websocket.send_json({"type": "ready", "session_id": session_id, "state": state})
    audit("REALTIME_CONNECTED", {"state": state})

    buffer = UtteranceBuffer(config)
    stt = _StreamingInput(streaming, config.sample_rate, audit)
    listening = False
    pending_start = False
    playback_tracking = False
    audio_feedback = False
    listening_since = perf_counter()
    last_frame_at: float | None = None
    received_frames = 0
    no_speech_sent = False
    calibration_recorded = not buffer.calibrating
    closed = False
    generation = 0
    playback_generation: int | None = None
    generation_done = False
    output_task: asyncio.Task | None = None
    output_tasks: set[asyncio.Task] = set()
    ack_tasks: dict[tuple[int, str], asyncio.Task] = {}
    ack_pcm: dict[tuple[int, str], bytes] = {}
    greeting_started = False
    tts_sample_rate = config.sample_rate
    dropped_frames = 0
    dropped_bytes = 0
    send_lock = asyncio.Lock()

    async def send(event, *, gen=None):
        async with send_lock:
            if closed or (gen is not None and gen != generation):
                return False
            if gen is not None:
                event = {**event, "generation_id": gen}
            await websocket.send_json(event)
            return True

    def record_drops():
        nonlocal dropped_frames, dropped_bytes
        if dropped_frames:
            audit(
                "REALTIME_FRAMES_DROPPED",
                {
                    "frames": dropped_frames,
                    "bytes": dropped_bytes,
                    "reason": "TURN_BUSY",
                },
            )
            dropped_frames = dropped_bytes = 0

    async def listen(*, keep_buffer=False):
        nonlocal listening, pending_start, listening_since, last_frame_at
        nonlocal received_frames, no_speech_sent
        pending_start = False
        if not listening:
            record_drops()
            listening = True
            if not keep_buffer:
                buffer.reset()
            stt.begin()
            listening_since = perf_counter()
            last_frame_at = listening_since if keep_buffer else None
            received_frames = 1 if keep_buffer else 0
            no_speech_sent = False
            audit(
                "VAD_LISTENING",
                {
                    "adaptive": config.adaptive_vad,
                    "threshold": round(buffer.threshold),
                    "calibrating": buffer.calibrating,
                },
            )
        await send({"type": "listening"})

    async def feedback():
        nonlocal no_speech_sent
        try:
            while True:
                await asyncio.sleep(0.25)
                if not listening:
                    continue
                now = perf_counter()
                if audio_feedback:
                    await send(
                        {
                            "type": "audio_level",
                            "rms": round(buffer.rms),
                            "noise_floor": round(buffer.noise_floor),
                            "threshold": round(buffer.threshold),
                            "calibrating": buffer.calibrating,
                            "noise_limited": buffer.noise_limited,
                            "received_frames": received_frames,
                            "speech": buffer._saw_speech and buffer.rms >= buffer._speech_threshold,
                        }
                    )
                elapsed_ms = round((now - listening_since) * 1000)
                if elapsed_ms < config.no_speech_ms or no_speech_sent:
                    continue
                no_speech_sent = True
                reason = "NO_SPEECH_DETECTED"
                if not received_frames:
                    reason = "NO_AUDIO_FRAMES"
                elif buffer.noise_limited:
                    reason = "EXCESSIVE_NOISE"
                elif last_frame_at is not None and now - last_frame_at > 0.5:
                    reason = "AUDIO_STREAM_STOPPED"
                payload = {
                    "reason": reason,
                    "elapsed_ms": elapsed_ms,
                    "rms": round(buffer.rms),
                    "threshold": round(buffer.threshold),
                }
                audit("VAD_NO_SPEECH", payload)
                if audio_feedback:
                    await send({"type": "no_speech", **payload})
        except Exception as error:
            audit(
                "REALTIME_TASK_FAILED",
                {"phase": "audio_feedback", "error_type": type(error).__name__},
            )
            raise

    async def invalidate(reason, *, notify=True):
        nonlocal generation, playback_generation, generation_done, output_task
        old_generation = playback_generation if playback_generation is not None else generation
        active = playback_generation is not None or (
            output_task is not None and not output_task.done()
        )
        generation += 1
        playback_generation = None
        generation_done = False
        if output_task is not None:
            if not output_task.done() and not output_task.cancelling():
                output_task.cancel()
            output_task = None
        if active:
            audit(
                "REALTIME_OUTPUT_CANCELLED",
                {
                    "generation_id": old_generation,
                    "reason": reason,
                },
            )
            if notify:
                await send({"type": "stop_speaking", "generation_id": old_generation})

    def prepare_ack(rate, language="hi-en"):
        key = (rate, language)
        if key in ack_tasks or getattr(speech, "synthesize_pcm", None) is None:
            return

        async def run():
            try:
                ack_pcm[key] = await speech.synthesize_pcm(phrase("ack", language), sample_rate=rate, language=language)
            except GnaniError as error:
                audit("ACK_UNAVAILABLE", {"error_type": type(error).__name__})
            except Exception as error:
                audit("REALTIME_TASK_FAILED", {"phase": "ack", "error_type": type(error).__name__})
                raise

        ack_tasks[key] = asyncio.create_task(run())

    def start_output(job):
        nonlocal generation, generation_done, output_task, greeting_started
        generation += 1
        gen = generation
        generation_done = False
        rate = tts_sample_rate
        previous = tuple(output_tasks)
        greeting_started = True
        buffer.reset()

        async def run():
            nonlocal generation_done, playback_generation
            try:
                if previous:
                    await asyncio.gather(*previous, return_exceptions=True)
                if gen != generation or closed:
                    return
                await job(gen, rate)
            except asyncio.CancelledError:
                raise
            except DomainError as error:
                audit("REALTIME_TURN_FAILED", {"status_code": error.status_code})
                await send({"type": "error", "detail": error.detail}, gen=gen)
                if gen == generation and not closed:
                    generation_done = True
                    playback_generation = None
                    if pending_start:
                        await listen()
            except Exception as error:
                audit(
                    "REALTIME_TASK_FAILED", {"phase": "output", "error_type": type(error).__name__}
                )
                if gen == generation and not closed:
                    generation_done = True
                    playback_generation = None
                await send(
                    {"type": "error", "detail": "Voice turn failed; reset or reconnect."}, gen=gen
                )
                raise
            finally:
                record_drops()

        output_task = asyncio.create_task(run())
        output_tasks.add(output_task)

        def finished(task):
            output_tasks.discard(task)
            if not task.cancelled():
                task.exception()  # The supervisor already records unexpected failures.

        output_task.add_done_callback(finished)

    async def chunk_sink(chunk, gen, rate):
        nonlocal playback_generation
        if gen != generation or closed:
            raise asyncio.CancelledError
        playback_generation = gen
        if not await send(
            {
                "type": "agent_audio_chunk",
                "chunk_base64": base64.b64encode(chunk).decode("ascii"),
                "sample_rate": rate,
            },
            gen=gen,
        ):
            raise asyncio.CancelledError

    async def audio_end(event, gen):
        nonlocal generation_done, playback_generation
        if gen != generation or closed:
            raise asyncio.CancelledError
        generation_done = True
        await send({"type": "agent_audio_end", **event}, gen=gen)
        if pending_start and (not playback_tracking or playback_generation is None):
            if not playback_tracking:
                playback_generation = None
            await listen()

    async def greet(gen, rate):
        started = perf_counter()
        first_audio_ms = None
        byte_count = 0
        error = None
        stream_pcm = getattr(speech, "stream_pcm", None)
        try:
            if stream_pcm is None:
                error = "TTS_UNAVAILABLE"
            else:
                async for chunk in stream_pcm(OPENING_LINE, sample_rate=rate, language="hi-en"):
                    if chunk:
                        if first_audio_ms is None:
                            first_audio_ms = round((perf_counter() - started) * 1000)
                        byte_count += len(chunk)
                        await chunk_sink(chunk, gen, rate)
        except GnaniError:
            error = "TTS_UNAVAILABLE" if not byte_count else "TTS_INTERRUPTED"
        await audio_end(
            {
                "audio_streamed": byte_count > 0,
                "audio_error": error,
                "ack_played": False,
                "timings": {
                    "first_audio_ms": first_audio_ms or 0,
                    "tts_ms": round((perf_counter() - started) * 1000),
                    "total_ms": round((perf_counter() - started) * 1000),
                },
            },
            gen,
        )
        audit(
            "OPENING_LINE_SPOKEN",
            {
                "bytes": byte_count,
                "first_audio_ms": first_audio_ms,
                "error": error,
            },
        )
        prepare_ack(rate)

    async def deliver(
        gen, rate, *, pcm=None, capture=None, text=None, language=None, ended_at=None
    ):
        nonlocal playback_generation
        with Session(bind) as turn_db:
            service = VoiceTurnService(
                turn_db,
                llm,
                speech,
                max_audio_bytes=max_audio_bytes,
                min_confidence=min_confidence,
                min_transcript_confidence=min_transcript_confidence,
            )
            for (ack_rate, ack_language), data in ack_pcm.items():
                service.set_ack(data, ack_rate, ack_language)

            async def turn_sink(response):
                if not await send(
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
                        "stt_fallback_reason": capture.fallback_reason
                        if capture is not None
                        else None,
                    },
                    gen=gen,
                ):
                    raise asyncio.CancelledError

            async def audio_sink(chunk):
                await chunk_sink(chunk, gen, rate)

            transcript = await stt.finish(capture) if pcm is not None else None
            if gen != generation or closed:
                raise asyncio.CancelledError
            if text is not None or transcript is not None:
                response = await service.handle_transcript(
                    session_id,
                    text if text is not None else transcript.text,
                    detected_language=language if text is not None else transcript.language,
                    transcript_confidence=None,
                    stt_ms=0 if text is not None else transcript.latency_ms,
                    stt_mode="scripted" if text is not None else "streaming",
                    total_started=ended_at,
                    turn_started=ended_at,
                    turn_sink=turn_sink,
                    audio_sink=audio_sink,
                    sample_rate=rate,
                )
            else:
                response = await service.handle_audio(
                    session_id,
                    pcm16_to_wav(pcm, config.sample_rate),
                    "utterance.wav",
                    "audio/wav",
                    turn_sink=turn_sink,
                    audio_sink=audio_sink,
                    sample_rate=rate,
                    turn_started=ended_at,
                )
            if response.audio_base64:
                if gen != generation or closed:
                    raise asyncio.CancelledError
                playback_generation = gen
                await send(
                    {
                        "type": "agent_audio",
                        "audio_base64": response.audio_base64,
                        "content_type": response.audio_content_type,
                    },
                    gen=gen,
                )
            await audio_end(
                {
                    "audio_streamed": response.audio_streamed,
                    "audio_error": response.audio_error,
                    "ack_played": response.ack_played,
                    "timings": response.timings.model_dump(mode="json"),
                },
                gen,
            )
            prepare_ack(rate, response.planner.response_language)

    async def endpoint():
        nonlocal listening
        listening = False
        pcm, duration, capture = bytes(buffer.pcm), buffer.speech_ms, stt.capture
        details = {
            "reason": buffer.end_reason or "PUSH_TO_TALK",
            "speech_ms": duration,
            "buffered_ms": round(len(pcm) * 1000 / (config.sample_rate * 2)),
        }
        audit("VAD_UTTERANCE_ENDED", details)
        buffer.reset()
        ended_at = perf_counter()
        await send({"type": "speech_end", "duration_ms": duration})

        async def job(gen, rate):
            await deliver(gen, rate, pcm=pcm, capture=capture, ended_at=ended_at)

        start_output(job)

    feedback_task = asyncio.create_task(feedback())
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                audit("REALTIME_DISCONNECTED", {"code": message.get("code")})
                break
            if message.get("bytes") is not None:
                frame = message["bytes"]
                if playback_generation is not None:
                    if buffer.add(frame, calibrate=False, start_ms=config.barge_in_ms):
                        await invalidate("BARGE_IN")
                        stt.abort()
                        listening = False
                        audit(
                            "BARGE_IN",
                            {
                                "rms": round(buffer.rms),
                                "sustained_ms": buffer.speech_ms,
                                "threshold": round(buffer.threshold),
                            },
                        )
                        await listen(keep_buffer=True)
                        stt.offer(bytes(buffer.pcm))
                        await send({"type": "speech_start"})
                    continue
                if output_task is not None and not output_task.done():
                    dropped_frames += 1
                    dropped_bytes += len(frame)
                    continue
                if not listening:
                    continue
                if not received_frames:
                    listening_since = perf_counter()
                    no_speech_sent = False
                received_frames += 1
                last_frame_at = perf_counter()
                before = len(buffer.pcm)
                if buffer.add(frame):
                    stt.offer(bytes(buffer.pcm))
                    audit(
                        "VAD_SPEECH_STARTED",
                        {
                            "threshold": round(buffer.threshold),
                            "noise_floor": round(buffer.noise_floor),
                            "sustained_ms": buffer.speech_ms,
                        },
                    )
                    await send({"type": "speech_start"})
                elif buffer._saw_speech:
                    stt.offer(frame[: len(buffer.pcm) - before])
                if not calibration_recorded and not buffer.calibrating:
                    calibration_recorded = True
                    audit(
                        "VAD_CALIBRATED",
                        {
                            "noise_floor": round(buffer.noise_floor),
                            "threshold": round(buffer.threshold),
                        },
                    )
                if buffer.rejected_ms:
                    audit(
                        "VAD_ONSET_REJECTED",
                        {
                            "speech_ms": buffer.rejected_ms,
                            "reason": "INSUFFICIENT_SUSTAINED_ENERGY",
                        },
                    )
                if buffer.has_utterance and buffer.rms >= buffer._speech_threshold:
                    listening_since = perf_counter()
                    no_speech_sent = False
                if buffer.complete:
                    if buffer.has_utterance:
                        await endpoint()
                    else:
                        audit("VAD_SHORT_UTTERANCE", {"speech_ms": buffer.speech_ms})
                        if audio_feedback:
                            await send(
                                {
                                    "type": "no_speech",
                                    "reason": "SPEECH_TOO_SHORT",
                                    "elapsed_ms": buffer.speech_ms,
                                }
                            )
                        stt.abort()
                        buffer.reset()
                        stt.begin()
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                control = json.loads(raw)
            except json.JSONDecodeError:
                control = None
            if not isinstance(control, dict):
                await send({"type": "error", "detail": "Invalid control message"})
                continue
            kind = control.get("type")
            if kind == "config":
                requested = control.get("tts_sample_rate")
                if (output_task is None or output_task.done()) and (
                    isinstance(requested, int)
                    and not isinstance(requested, bool)
                    and MIN_TTS_SAMPLE_RATE <= requested <= MAX_TTS_SAMPLE_RATE
                ):
                    tts_sample_rate = requested
                playback_tracking = control.get("playback_tracking") is True or playback_tracking
                audio_feedback = control.get("audio_feedback") is True or audio_feedback
                await send({"type": "config_ack", "tts_sample_rate": tts_sample_rate})
                if not greeting_started:
                    start_output(greet)
            elif kind == "start":
                if (output_task is not None and not output_task.done()) or (
                    playback_tracking and playback_generation is not None
                ):
                    pending_start = True
                else:
                    playback_generation = None
                    await listen()
            elif kind == "playback_done":
                acknowledged = control.get("generation_id")
                if (
                    type(acknowledged) is int
                    and acknowledged == playback_generation
                    and generation_done
                ):
                    playback_generation = None
                    audit("REALTIME_PLAYBACK_DONE", {"generation_id": acknowledged})
                    if pending_start:
                        await listen()
            elif kind == "stop":
                pending_start = False
                if listening and buffer.has_utterance:
                    await endpoint()
                elif listening:
                    listening = False
                    if buffer._saw_speech:
                        audit(
                            "VAD_SHORT_UTTERANCE",
                            {"speech_ms": buffer.speech_ms, "reason": "PUSH_TO_TALK_RELEASE"},
                        )
                    buffer.reset()
                    stt.abort()
            elif kind == "reset":
                pending_start = False
                listening = False
                buffer.reset()
                await invalidate("RESET")
                stt.abort()
            elif kind == "scripted_turn":
                text = control.get("text")
                if not isinstance(text, str) or not text.strip():
                    await send({"type": "error", "detail": "scripted_turn needs a text field"})
                    continue
                if output_task is not None and not output_task.done():
                    await send({"type": "error", "detail": "A voice turn is already in progress"})
                    continue
                await invalidate("SCRIPTED_TURN")
                listening = False
                stt.abort()
                buffer.reset()
                await send({"type": "speech_end", "duration_ms": 0, "scripted": True})
                start_output(
                    partial(
                        deliver,
                        text=text.strip(),
                        language=control.get("detected_language"),
                        ended_at=perf_counter(),
                    )
                )
            else:
                await send({"type": "error", "detail": "Unknown control message"})
    except WebSocketDisconnect:
        audit("REALTIME_DISCONNECTED", {"code": "client"})
    except asyncio.CancelledError:
        audit("REALTIME_DISCONNECTED", {"code": "server_cancelled"})
        raise
    finally:
        closed = True
        # ASGI cancellation must not release socket ownership before cleanup ends.
        with CancelScope(shield=True):
            await invalidate("DISCONNECT", notify=False)
            tasks = list(output_tasks) + list(ack_tasks.values()) + [feedback_task]
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await stt.close()
            record_drops()
