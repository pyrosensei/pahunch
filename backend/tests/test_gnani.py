import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.gnani import (
    GnaniResponseError,
    GnaniSpeechClient,
    GnaniTimeoutError,
    build_gnani_speech_client,
)


def make_client(handler) -> GnaniSpeechClient:
    return GnaniSpeechClient(
        api_key="test-key",
        base_url="https://gnani.example",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )


def test_stt_sends_multipart_and_parses_transcript():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key-id")
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "transcript": " haan theek hai ",
                "detected_language_code": "hi-IN",
                "processing_time": 0.4,
            },
        )

    transcript = asyncio.run(
        make_client(handler).transcribe(
            b"RIFFDATA",
            filename="audio.wav",
            content_type="audio/wav",
        )
    )

    assert captured["url"] == "https://gnani.example/stt/v3"
    assert captured["key"] == "test-key"
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    body = captured["body"]
    assert b'name="audio_file"' in body
    assert b'filename="audio.wav"' in body
    assert b'name="model"' in body
    assert b"prisma-v3.0" in body
    assert b'name="language_code"' in body
    assert b"indic-auto" in body
    assert b"RIFFDATA" in body
    assert transcript.text == "haan theek hai"
    assert transcript.language == "hi-IN"
    assert transcript.confidence is None
    assert transcript.latency_ms >= 0


def test_stt_uses_optional_confidence_only_on_zero_to_one_scale():
    responses = [
        {"transcript": "haan", "confidence": 0.81},
        {"transcript": "haan", "confidence": 81},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    client = make_client(handler)
    first = asyncio.run(
        client.transcribe(b"a", filename="audio.wav", content_type="audio/wav")
    )
    second = asyncio.run(
        client.transcribe(b"a", filename="audio.wav", content_type="audio/wav")
    )

    assert first.confidence == 0.81
    assert second.confidence is None


def test_stt_rejects_malformed_responses():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "wrong field"})

    with pytest.raises(GnaniResponseError, match="no transcript"):
        asyncio.run(
            make_client(handler).transcribe(
                b"a",
                filename="audio.wav",
                content_type="audio/wav",
            )
        )


def test_tts_sends_approved_text_and_returns_audio():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key-id")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"RIFFAUDIO",
            headers={"content-type": "audio/mpeg"},
        )

    audio = asyncio.run(make_client(handler).synthesize("Theek hai, direct handoff."))

    assert captured["url"] == "https://gnani.example/api/v1/tts/inference"
    assert captured["key"] == "test-key"
    assert captured["payload"] == {
        "text": "Theek hai, direct handoff.",
        "voice": "Shivani",
        "model": "timbre-v3.0",
        "language": "hi-en",
        "audio_config": {"container": "mp3"},
    }
    assert audio.data == b"RIFFAUDIO"
    assert audio.content_type == "audio/mpeg"
    assert audio.latency_ms >= 0


def test_tts_container_can_be_overridden():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=b"RIFFAUDIO", headers={"content-type": "audio/wav"})

    asyncio.run(make_client(handler).synthesize("Hello", container="wav"))

    assert captured["payload"]["audio_config"] == {"container": "wav"}


def test_tts_rejects_json_and_empty_audio():
    responses = [
        httpx.Response(200, json={"error": "bad request"}),
        httpx.Response(200, content=b"", headers={"content-type": "audio/wav"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = make_client(handler)
    with pytest.raises(GnaniResponseError, match="non-audio"):
        asyncio.run(client.synthesize("hello"))
    with pytest.raises(GnaniResponseError, match="empty audio"):
        asyncio.run(client.synthesize("hello"))


def test_stt_timeout_and_http_errors_are_mapped():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow provider", request=request)

    with pytest.raises(GnaniTimeoutError):
        asyncio.run(
            make_client(timeout).transcribe(
                b"a",
                filename="audio.wav",
                content_type="audio/wav",
            )
        )

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    with pytest.raises(GnaniResponseError, match="429"):
        asyncio.run(
            make_client(rate_limited).transcribe(
                b"a",
                filename="audio.wav",
                content_type="audio/wav",
            )
        )


def test_build_client_requires_an_api_key():
    assert build_gnani_speech_client(Settings(_env_file=None, gnani_api_key="")) is None
    client = build_gnani_speech_client(Settings(_env_file=None, gnani_api_key="secret"))
    assert isinstance(client, GnaniSpeechClient)
