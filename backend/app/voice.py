from __future__ import annotations

import base64
import json
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

    async def handle_audio(
        self,
        session_id: str,
        audio: bytes,
        filename: str,
        content_type: str,
    ) -> VoiceTurnResponse:
        started = perf_counter()
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
    ) -> VoiceTurnResponse:
        started = perf_counter() if total_started is None else total_started
        call = self.calls.get_call(session_id)
        if call.state not in PLANNER_STATES:
            raise DomainError(
                409,
                f"Voice turns are not available in call state {call.state.value}",
            )

        text = text.strip()
        if not text:
            raise DomainError(422, "No speech was detected in the audio.")
        if len(text) > 2_000:
            raise DomainError(422, "The transcript is too long for one turn.")

        planner_started = perf_counter()
        planner_response = await self.planner.handle_turn(
            session_id,
            AgentTurnRequest(
                text=text,
                transcript_confidence=transcript_confidence,
            ),
        )
        planner_ms = round((perf_counter() - planner_started) * 1_000)

        audio_base64 = None
        audio_type = None
        audio_error = None
        audio_bytes = 0
        tts_started = perf_counter()
        # The planner may already have committed state; a TTS failure must not
        # turn this into an error that makes the client resubmit the turn.
        try:
            speech_audio = await self.speech.synthesize(planner_response.agent_message)
        except GnaniTimeoutError:
            audio_error = "TTS_TIMEOUT"
        except GnaniResponseError:
            audio_error = "TTS_INVALID_RESPONSE"
        except GnaniGatewayError:
            audio_error = "TTS_UNAVAILABLE"
        else:
            audio_base64 = base64.b64encode(speech_audio.data).decode("ascii")
            audio_type = speech_audio.content_type
            audio_bytes = len(speech_audio.data)
        tts_ms = round((perf_counter() - tts_started) * 1_000)
        total_ms = round((perf_counter() - started) * 1_000)

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
                        "detected_language": detected_language,
                        "transcript_confidence": transcript_confidence,
                        "transcript_chars": len(text),
                        "audio_error": audio_error,
                        "audio_bytes": audio_bytes,
                    },
                    default=str,
                ),
                created_at=utc_now(),
            )
        )
        self.db.commit()

        return VoiceTurnResponse(
            session_id=session_id,
            state=planner_response.state,
            transcript=text,
            detected_language=detected_language,
            transcript_confidence=transcript_confidence,
            agent_message=planner_response.agent_message,
            turn=planner_response.turn,
            planner=planner_response.planner,
            audio_base64=audio_base64,
            audio_content_type=audio_type,
            audio_error=audio_error,
            timings=VoiceTimings(
                stt_ms=stt_ms,
                planner_ms=planner_ms,
                tts_ms=tts_ms,
                total_ms=total_ms,
            ),
        )
