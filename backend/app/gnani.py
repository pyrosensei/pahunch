from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import httpx

from app.config import Settings


class GnaniError(Exception):
    pass


class GnaniGatewayError(GnaniError):
    pass


class GnaniTimeoutError(GnaniGatewayError):
    pass


class GnaniResponseError(GnaniGatewayError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None
    latency_ms: int = 0


@dataclass(frozen=True)
class SpeechAudio:
    data: bytes
    content_type: str
    latency_ms: int


class SpeechClient(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Transcript: ...

    async def synthesize(self, text: str, *, container: str = "mp3") -> SpeechAudio: ...


def _optional_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    return confidence if 0 <= confidence <= 1 else None


class GnaniSpeechClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.vachana.ai",
        stt_model: str = "prisma-v3.0",
        stt_language: str = "indic-auto",
        tts_model: str = "timbre-v3.0",
        tts_voice: str = "Shivani",
        tts_language: str = "hi-en",
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._stt_model = stt_model
        self._stt_language = stt_language
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._tts_language = tts_language
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Transcript:
        started = perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/stt/v3",
                    headers={"X-API-Key-ID": self._api_key},
                    data={
                        "model": self._stt_model,
                        "language_code": self._stt_language,
                    },
                    files={"audio_file": (filename, audio, content_type)},
                )
        except httpx.TimeoutException as error:
            raise GnaniTimeoutError("Gnani STT timed out") from error
        except httpx.HTTPError as error:
            raise GnaniGatewayError("Gnani STT request failed") from error

        if response.status_code >= 400:
            raise GnaniResponseError(f"Gnani STT returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as error:
            raise GnaniResponseError("Gnani STT returned a non-JSON response") from error
        if not isinstance(payload, dict):
            raise GnaniResponseError("Gnani STT returned an unexpected response")

        text = payload.get("transcript")
        if not isinstance(text, str):
            raise GnaniResponseError("Gnani STT response has no transcript")

        language = payload.get("detected_language_code")
        confidence = _optional_confidence(payload.get("confidence"))
        if confidence is None:
            confidence = _optional_confidence(payload.get("transcript_confidence"))

        return Transcript(
            text=text.strip(),
            language=language if isinstance(language, str) else None,
            confidence=confidence,
            latency_ms=round((perf_counter() - started) * 1_000),
        )

    async def synthesize(self, text: str, *, container: str = "mp3") -> SpeechAudio:
        started = perf_counter()
        payload = {
            "text": text,
            "voice": self._tts_voice,
            "model": self._tts_model,
            "language": self._tts_language,
            # MP3 keeps the WebSocket agent_audio frames an order of magnitude
            # smaller than WAV and decodes fine in browsers.
            "audio_config": {"container": container},
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/api/v1/tts/inference",
                    headers={
                        "X-API-Key-ID": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as error:
            raise GnaniTimeoutError("Gnani TTS timed out") from error
        except httpx.HTTPError as error:
            raise GnaniGatewayError("Gnani TTS request failed") from error

        if response.status_code >= 400:
            raise GnaniResponseError(f"Gnani TTS returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if "json" in content_type or content_type.startswith("text/"):
            raise GnaniResponseError("Gnani TTS returned a non-audio response")
        if not response.content:
            raise GnaniResponseError("Gnani TTS returned empty audio")
        if not content_type.startswith("audio/"):
            content_type = "audio/wav"

        return SpeechAudio(
            data=response.content,
            content_type=content_type,
            latency_ms=round((perf_counter() - started) * 1_000),
        )


def build_gnani_speech_client(settings: Settings) -> GnaniSpeechClient | None:
    if not settings.gnani_api_key:
        return None
    return GnaniSpeechClient(
        api_key=settings.gnani_api_key,
        base_url=settings.gnani_base_url,
        stt_model=settings.gnani_stt_model,
        stt_language=settings.gnani_stt_language,
        tts_model=settings.gnani_tts_model,
        tts_voice=settings.gnani_tts_voice,
        tts_language=settings.gnani_tts_language,
        timeout_seconds=settings.gnani_timeout_seconds,
    )
