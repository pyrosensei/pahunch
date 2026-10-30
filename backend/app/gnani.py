from __future__ import annotations

import asyncio
import base64
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import httpx

from app.config import Settings
from app.http_pool import HttpClientPool

# The Gnani API allows one active request per API key and answers 429 for
# every other. Requests are therefore serialised behind a single slot and
# retried with backoff instead of failing the turn.
GNANI_RETRY_DELAYS = (0.4, 0.8, 1.6, 3.2, 5.0, 8.0, 12.0)


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


def split_api_keys(primary: str, extra: str = "") -> list[str]:
    """Ordered, de-duplicated list of usable Gnani API keys."""
    keys = [primary.strip()] if primary.strip() else []
    keys += [item.strip() for item in extra.split(",") if item.strip()]
    seen: set[str] = set()
    ordered = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


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
        extra_api_keys: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_keys = split_api_keys(api_key, extra_api_keys) or [api_key]
        self._key_index = 0
        # One slot per key: the provider rejects a second concurrent request
        # on the same key with 429.
        self._gate = asyncio.Semaphore(len(self._api_keys))
        self._base_url = base_url.rstrip("/")
        self._stt_model = stt_model
        self._stt_language = stt_language
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._tts_language = tts_language
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._pool = HttpClientPool(timeout=timeout_seconds, transport=transport)
        self._tts_cache: dict[tuple[str, str], SpeechAudio] = {}
        self._pcm_cache: dict[tuple[str, int], bytes] = {}

    def _next_key(self) -> str:
        key = self._api_keys[self._key_index % len(self._api_keys)]
        self._key_index += 1
        return key

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send a Gnani request, retrying while the provider reports 429.

        The provider allows one active request per API key, so a rate-limited
        call rotates to the next key and backs off instead of failing the turn.
        """
        async with self._gate:
            headers = dict(kwargs.pop("headers", None) or {})
            for attempt in range(len(GNANI_RETRY_DELAYS) + 1):
                response = await self._pool.get().request(
                    method,
                    url,
                    headers={**headers, "X-API-Key-ID": self._next_key()},
                    **kwargs,
                )
                if response.status_code != 429:
                    return response
                if attempt < len(GNANI_RETRY_DELAYS):
                    delay = GNANI_RETRY_DELAYS[attempt]
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.3))
            raise GnaniResponseError("Gnani request returned HTTP 429 when rate limited")

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Transcript:
        started = perf_counter()
        try:
            response = await self._request(
                "POST",
                f"{self._base_url}/stt/v3",
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
        key = (text, container)
        cached = self._tts_cache.get(key)
        if cached is not None:
            return SpeechAudio(
                data=cached.data,
                content_type=cached.content_type,
                latency_ms=round((perf_counter() - started) * 1_000),
            )
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
            response = await self._request(
                "POST",
                f"{self._base_url}/api/v1/tts/inference",
                headers={
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

        audio = SpeechAudio(
            data=response.content,
            content_type=content_type,
            latency_ms=round((perf_counter() - started) * 1_000),
        )
        self._tts_cache[key] = audio
        return audio

    async def stream_pcm(
        self,
        text: str,
        *,
        sample_rate: int = 16_000,
    ) -> AsyncIterator[bytes]:
        """Yield raw PCM16 chunks as Timbre synthesises them.

        The SSE endpoint starts delivering audio ~400 ms after the request,
        well before the whole sentence exists, so the client can play the
        agent while it is still being spoken.
        """
        payload = {
            "text": text,
            "voice": self._tts_voice,
            "model": self._tts_model,
            "language": self._tts_language,
            "audio_config": {"container": "raw", "sample_rate": sample_rate},
        }
        for attempt in range(len(GNANI_RETRY_DELAYS) + 1):
            rate_limited = False
            try:
                async with self._gate:
                    headers = {
                        "X-API-Key-ID": self._next_key(),
                        "Content-Type": "application/json",
                    }
                    async with self._pool.get().stream(
                        "POST",
                        f"{self._base_url}/api/v1/tts/sse",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code == 429:
                            rate_limited = True
                        else:
                            if response.status_code >= 400:
                                raise GnaniResponseError(
                                    f"Gnani TTS returned HTTP {response.status_code}"
                                )
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                try:
                                    event = json.loads(line[5:].strip())
                                except ValueError:
                                    continue
                                audio = event.get("audio")
                                if isinstance(audio, str) and audio:
                                    yield base64.b64decode(audio)
                if not rate_limited:
                    return
            except httpx.TimeoutException as error:
                raise GnaniTimeoutError("Gnani TTS timed out") from error
            except httpx.HTTPError as error:
                raise GnaniGatewayError("Gnani TTS request failed") from error
            if attempt < len(GNANI_RETRY_DELAYS):
                delay = GNANI_RETRY_DELAYS[attempt]
                await asyncio.sleep(delay + random.uniform(0, delay * 0.3))
        raise GnaniResponseError("Gnani TTS stream returned HTTP 429 when rate limited")

    async def synthesize_pcm_blocking(self, text: str, *, sample_rate: int = 16_000) -> bytes:
        """Whole-utterance PCM16 from the non-streaming endpoint.

        This is the fallback for the SSE stream: it returns the complete
        sentence in one response, so the caller still hears the agent when
        streaming fails.
        """
        payload = {
            "text": text,
            "voice": self._tts_voice,
            "model": self._tts_model,
            "language": self._tts_language,
            "audio_config": {"container": "raw", "sample_rate": sample_rate},
        }
        try:
            response = await self._request(
                "POST",
                f"{self._base_url}/api/v1/tts/inference",
                headers={
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
        if not response.content:
            raise GnaniResponseError("Gnani TTS returned empty audio")
        return response.content

    async def aclose(self) -> None:
        await self._pool.aclose()

    async def synthesize_pcm(self, text: str, *, sample_rate: int = 16_000) -> bytes:
        """Whole-utterance PCM16, cached so it can be replayed for free."""
        key = (text, sample_rate)
        cached = self._pcm_cache.get(key)
        if cached is not None:
            return cached
        chunks = [chunk async for chunk in self.stream_pcm(text, sample_rate=sample_rate)]
        data = b"".join(chunks)
        if not data:
            raise GnaniResponseError("Gnani TTS returned no audio")
        self._pcm_cache[key] = data
        return data


def build_gnani_speech_client(
    settings: Settings, *, extra_api_keys: str | None = None
) -> GnaniSpeechClient | None:
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
        extra_api_keys=(
            settings.gnani_api_keys if extra_api_keys is None else extra_api_keys
        ),
    )
