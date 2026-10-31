from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from sqlmodel import Session

from app.calls import CallService, utc_now
from app.errors import DomainError
from app.gnani import (
    GnaniGatewayError,
    GnaniResponseError,
    GnaniTimeoutError,
    SpeechClient,
)
from app.language import redact_customer_text, response_language
from app.llm import StructuredLLM
from app.models import AuditEvent
from app.planner import (
    MIN_CONFIDENCE,
    MIN_TRANSCRIPT_CONFIDENCE,
    PLANNER_STATES,
    PlannerService,
)
from app.schemas import AgentTurnRequest, VoiceTimings, VoiceTurnResponse

MAX_AUDIO_BYTES = 10_000_000

# The planner is a remote LLM call that cannot be made faster than the
# provider allows. A short, pre-synthesised acknowledgement is played while it
# runs so the caller hears the agent within a second instead of dead air. It
# makes no promise about timing, only that the agent is working.
ACK_PHRASE = "Ji, main check kar rahi hoon."
ACK_DELAY_SECONDS = 0.55

# The realtime socket pushes the agent's text before its audio so the caller
# can show the reply while the first audio chunk is still being synthesised.
TurnSink = Callable[[VoiceTurnResponse], Awaitable[None]]
AudioSink = Callable[[bytes], Awaitable[None]]

_AUDIO_EXTENSIONS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/aac": "aac",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
}
_SUFFIX_EXTENSIONS = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".flac": "flac",
    ".aac": "aac",
    ".m4a": "m4a",
    ".mp4": "m4a",
}
_EXTENSION_CONTENT_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
}


def _resolve_audio_type(filename: str, content_type: str) -> tuple[str, str]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    extension = _AUDIO_EXTENSIONS.get(normalized)
    if extension is None and normalized in {"", "application/octet-stream"}:
        extension = _SUFFIX_EXTENSIONS.get(Path(filename).suffix.lower())
    if extension is None:
        raise DomainError(
            415,
            "Unsupported audio format. Send WAV, MP3, OGG, FLAC, AAC, or M4A.",
        )
    return extension, _EXTENSION_CONTENT_TYPES[extension]


@dataclass(frozen=True)
class SpokenAudio:
    """Outcome of speaking the agent's reply."""

    size: int = 0
    streamed: bool = False
    error: str | None = None
    detail: str | None = None


class VoiceTurnService:
    def __init__(
        self,
        db: Session,
        llm: StructuredLLM,
        speech: SpeechClient,
        *,
        max_audio_bytes: int = MAX_AUDIO_BYTES,
        min_confidence: float = MIN_CONFIDENCE,
        min_transcript_confidence: float = MIN_TRANSCRIPT_CONFIDENCE,
    ):
        self.db = db
        self.speech = speech
        self.max_audio_bytes = max_audio_bytes
        self.calls = CallService(db)
        self.planner = PlannerService(
            db,
            llm,
            min_confidence=min_confidence,
            min_transcript_confidence=min_transcript_confidence,
        )
        self._acks: dict[tuple[int, str], bytes] = {}

    def set_ack(self, pcm: bytes, sample_rate: int, language: str = "hi-en") -> None:
        self._acks[(sample_rate, language)] = pcm

    def _ack_for(self, sample_rate: int, language: str) -> bytes | None:
        return self._acks.get((sample_rate, language))

    async def _speak(
        self,
        text: str,
        *,
        sample_rate: int,
        audio_sink: AudioSink,
        mark_first_audio: Callable[[], None],
        language: str,
    ) -> SpokenAudio:
        """Speak the agent's reply, falling back to a blocking synthesis.

        The caller must always hear the agent, so a failed stream is retried
        once and then falls back to the non-streaming endpoint, which returns
        the whole sentence at once. Once a chunk has reached the caller the
        fallback is skipped: repeating audio is worse than a truncated
        sentence.
        """
        stream_pcm = getattr(self.speech, "stream_pcm", None)
        if stream_pcm is not None:
            for _ in range(2):
                size = 0
                try:
                    async for chunk in stream_pcm(text, sample_rate=sample_rate, language=language):
                        if not chunk:
                            continue
                        size += len(chunk)
                        mark_first_audio()
                        await audio_sink(chunk)
                except GnaniTimeoutError as error:
                    code, detail = "TTS_TIMEOUT", str(error)
                except GnaniResponseError as error:
                    code, detail = "TTS_INVALID_RESPONSE", str(error)
                except GnaniGatewayError as error:
                    code, detail = "TTS_UNAVAILABLE", str(error)
                else:
                    if size:
                        return SpokenAudio(size=size, streamed=True)
                    code, detail = "TTS_EMPTY_STREAM", "stream returned no audio"
                if size:
                    return SpokenAudio(size=size, streamed=True, error=code, detail=detail)
                if code == "TTS_TIMEOUT":
                    break

        blocking = getattr(self.speech, "synthesize_pcm_blocking", None)
        if blocking is None:
            return SpokenAudio(error="TTS_UNAVAILABLE", detail="no blocking synthesis")
        try:
            data = await blocking(text, sample_rate=sample_rate, language=language)
        except GnaniTimeoutError as error:
            return SpokenAudio(error="TTS_TIMEOUT", detail=str(error))
        except GnaniResponseError as error:
            return SpokenAudio(error="TTS_INVALID_RESPONSE", detail=str(error))
        except GnaniGatewayError as error:
            return SpokenAudio(error="TTS_UNAVAILABLE", detail=str(error))
        mark_first_audio()
        await audio_sink(data)
        return SpokenAudio(size=len(data), streamed=True)

    async def handle_audio(
        self,
        session_id: str,
        audio: bytes,
        filename: str,
        content_type: str,
        *,
        turn_sink: TurnSink | None = None,
        audio_sink: AudioSink | None = None,
        sample_rate: int = 16_000,
        turn_started: float | None = None,
    ) -> VoiceTurnResponse:
        started = perf_counter() if turn_started is None else turn_started
        call = self.calls.get_call(session_id)
        if call.state not in PLANNER_STATES:
            raise DomainError(
                409,
                f"Voice turns are not available in call state {call.state.value}",
            )
        if not audio:
            raise DomainError(422, "The audio file is empty.")
        if len(audio) > self.max_audio_bytes:
            raise DomainError(413, "The audio file is too large.")

        extension, canonical_type = _resolve_audio_type(filename, content_type)
        try:
            transcript = await self.speech.transcribe(
                audio,
                filename=f"audio.{extension}",
                content_type=canonical_type,
            )
        except GnaniTimeoutError as error:
            raise DomainError(504, "Speech recognition timed out.") from error
        except GnaniResponseError as error:
            raise DomainError(
                502,
                "Speech recognition returned an unexpected response.",
            ) from error
        except GnaniGatewayError as error:
            raise DomainError(503, "Speech recognition is unavailable.") from error

        return await self.handle_transcript(
            session_id,
            transcript.text,
            detected_language=transcript.language,
            transcript_confidence=transcript.confidence,
            stt_ms=transcript.latency_ms,
            stt_mode="rest",
            total_started=started,
            turn_sink=turn_sink,
            audio_sink=audio_sink,
            sample_rate=sample_rate,
        )

    async def handle_transcript(
        self,
        session_id: str,
        text: str,
        *,
        detected_language: str | None,
        transcript_confidence: float | None,
        stt_ms: int,
        stt_mode: str,
        total_started: float | None = None,
        turn_started: float | None = None,
        turn_sink: TurnSink | None = None,
        audio_sink: AudioSink | None = None,
        sample_rate: int = 16_000,
    ) -> VoiceTurnResponse:
        started = perf_counter() if total_started is None else total_started
        turn_reference = started if turn_started is None else turn_started
        call = self.calls.get_call(session_id)
        if call.state not in PLANNER_STATES:
            raise DomainError(
                409,
                f"Voice turns are not available in call state {call.state.value}",
            )

        text = redact_customer_text(text.strip())
        if not text:
            raise DomainError(422, "No speech was detected in the audio.")
        if len(text) > 2_000:
            raise DomainError(422, "The transcript is too long for one turn.")

        planner_started = perf_counter()
        planner_task = asyncio.create_task(
            self.planner.handle_turn(
                session_id,
                AgentTurnRequest(
                    text=text,
                    transcript_confidence=transcript_confidence,
                ),
            )
        )
        ack_played = False
        first_audio_at = None
        language = response_language(text)
        ack = self._ack_for(sample_rate, language) if audio_sink is not None else None
        try:
            if ack is not None:
                # An acknowledgement does not transfer ownership of the planner task.
                done, _ = await asyncio.wait({planner_task}, timeout=ACK_DELAY_SECONDS)
                if not done:
                    first_audio_at = perf_counter()
                    await audio_sink(ack)
                    ack_played = True
            planner_response = await planner_task
        finally:
            if not planner_task.done():
                planner_task.cancel()
            await asyncio.gather(planner_task, return_exceptions=True)
        planner_ms = round((perf_counter() - planner_started) * 1_000)

        response = VoiceTurnResponse(
            session_id=session_id,
            state=planner_response.state,
            transcript=text,
            detected_language=detected_language,
            transcript_confidence=transcript_confidence,
            agent_message=planner_response.agent_message,
            turn=planner_response.turn,
            planner=planner_response.planner,
            timings=VoiceTimings(stt_ms=stt_ms, planner_ms=planner_ms),
        )
        if turn_sink is not None:
            await turn_sink(response)

        audio_base64 = None
        audio_type = None
        audio_error = None
        audio_error_detail = None
        audio_bytes = 0
        audio_streamed = False
        tts_started = perf_counter()

        def mark_first_audio() -> None:
            nonlocal first_audio_at
            if first_audio_at is None:
                first_audio_at = perf_counter()

        if audio_sink is not None and getattr(self.speech, "stream_pcm", None) is not None:
            # The planner may already have committed state; a TTS failure must
            # not turn this into an error that makes the client resubmit the
            # turn, so partial audio is still reported.
            spoken = await self._speak(
                planner_response.agent_message,
                sample_rate=sample_rate,
                audio_sink=audio_sink,
                mark_first_audio=mark_first_audio,
                language=planner_response.planner.response_language,
            )
            audio_bytes = spoken.size
            audio_streamed = spoken.streamed
            audio_error = spoken.error
            audio_error_detail = spoken.detail
            if audio_streamed:
                audio_type = f"audio/pcm;rate={sample_rate}"
        else:
            speech_audio = None
            for _ in range(2):
                try:
                    speech_audio = await self.speech.synthesize(
                        planner_response.agent_message,
                        language=planner_response.planner.response_language,
                    )
                except GnaniTimeoutError as error:
                    audio_error, audio_error_detail = "TTS_TIMEOUT", str(error)
                except GnaniResponseError as error:
                    audio_error, audio_error_detail = "TTS_INVALID_RESPONSE", str(error)
                except GnaniGatewayError as error:
                    audio_error, audio_error_detail = "TTS_UNAVAILABLE", str(error)
                else:
                    break
                if audio_error == "TTS_TIMEOUT":
                    break
            if speech_audio is not None:
                mark_first_audio()
                audio_base64 = base64.b64encode(speech_audio.data).decode("ascii")
                audio_type = speech_audio.content_type
                audio_bytes = len(speech_audio.data)
        tts_ms = round((perf_counter() - tts_started) * 1_000)
        total_ms = round((perf_counter() - started) * 1_000)
        if first_audio_at is not None:
            response.timings.first_audio_ms = round(
                (first_audio_at - turn_reference) * 1_000
            )

        response.audio_base64 = audio_base64
        response.audio_content_type = audio_type
        response.audio_error = audio_error
        response.audio_streamed = audio_streamed
        response.ack_played = ack_played
        response.timings.tts_ms = tts_ms
        response.timings.total_ms = total_ms

        self.db.add(
            AuditEvent(
                session_id=session_id,
                event_type="VOICE_TURN",
                payload_json=json.dumps(
                    {
                        "stt_ms": stt_ms,
                        "stt_mode": stt_mode,
                        "planner_ms": planner_ms,
                        "tts_ms": tts_ms,
                        "total_ms": total_ms,
                        "first_audio_ms": response.timings.first_audio_ms,
                        "audio_streamed": audio_streamed,
                        "ack_played": ack_played,
                        "detected_language": detected_language,
                        "response_language": planner_response.planner.response_language,
                        "wording_source": planner_response.planner.wording_source,
                        "fallback_reason": planner_response.planner.fallback_reason,
                        "transcript_confidence": transcript_confidence,
                        "transcript_chars": len(text),
                        "audio_error": audio_error,
                        "audio_error_detail": audio_error_detail,
                        "audio_bytes": audio_bytes,
                    },
                    default=str,
                ),
                created_at=utc_now(),
            )
        )
        self.db.commit()

        return response
