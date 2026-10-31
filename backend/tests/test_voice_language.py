import asyncio
import base64
import json

import httpx
import pytest
from test_planner import db as planner_database
from test_planner import start_call, valid_proposal
from test_realtime_a1 import voice
from test_voice_api import audio_file, start_session, voice_client

from app.gnani import GnaniResponseError, GnaniSpeechClient, SpeechAudio, Transcript
from app.llm import LLMCompletion
from app.voice import VoiceTurnService

db = planner_database


def proposal_reply():
    return {
        "action": "PROPOSE_PLAN",
        "message": "{plan_summary} {confirmation_question}",
        "proposal": valid_proposal(),
        "escalation_reason": None,
        "confidence": 0.95,
    }


class LanguageSpeech:
    def __init__(self, text, *, fail_stream=False):
        self.text = text
        self.fail_stream = fail_stream
        self.calls = []

    async def transcribe(self, audio, **kwargs):
        return Transcript(text=self.text, language="hi-IN", latency_ms=1)

    async def synthesize(self, text, *, container="mp3", language=None):
        self.calls.append(("synthesize", language))
        return SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=1)

    async def stream_pcm(self, text, *, sample_rate=16000, language=None):
        self.calls.append(("stream", language))
        if self.fail_stream:
            raise GnaniResponseError("test stream failure")
        yield b"\x01\x02"

    async def synthesize_pcm_blocking(self, text, *, sample_rate=16000, language=None):
        self.calls.append(("blocking", language))
        return b"\x03\x04"


@pytest.mark.parametrize(
    "text,language",
    [
        ("Please hand the parcel to me", "en-IN"),
        ("मुझे ही पार्सल देना", "hi-IN"),
        ("Mujhe hi parcel dena", "hi-en"),
    ],
)
def test_rest_voice_mirrors_transcript_instead_of_stt_tag(scripted_llm, text, language):
    speech = LanguageSpeech(text)
    with voice_client(scripted_llm, llm_outcomes=[proposal_reply()], speech=speech) as client:
        sid = start_session(client)
        response = client.post(f"/api/sessions/{sid}/voice-turn", files=audio_file())
        assert response.status_code == 200
        assert response.json()["planner"]["response_language"] == language
        assert speech.calls == [("synthesize", language)]


@pytest.mark.parametrize("fail_stream", [False, True])
def test_realtime_stream_and_blocking_fallback_receive_language(scripted_llm, fail_stream):
    async def run():
        speech = LanguageSpeech("Please hand it to me", fail_stream=fail_stream)
        async with voice(scripted_llm([proposal_reply()]), speech=speech) as (ws, _, _, _):
            ws.control("scripted_turn", text="Please hand it to me", detected_language="hi-IN")
            turn = await ws.event("turn")
            assert turn["planner"]["response_language"] == "en-IN"
            await ws.event("agent_audio_end")
            assert all(language == "en-IN" for _, language in speech.calls)
            assert (
                ("blocking", "en-IN") in speech.calls
                if fail_stream
                else speech.calls == [("stream", "en-IN")]
            )

    asyncio.run(run())


@pytest.mark.parametrize("method", ["synthesize", "synthesize_pcm", "synthesize_pcm_blocking"])
def test_gnani_payload_and_cache_are_language_specific(method):
    captured = []

    def handler(request):
        body = json.loads(request.content)
        captured.append(body["language"])
        if request.url.path.endswith("/sse"):
            event = json.dumps({"audio": base64.b64encode(b"PCM").decode()})
            return httpx.Response(200, text=f"data: {event}\n\n")
        return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/wav"})

    async def run():
        client = GnaniSpeechClient(api_key="test-key", transport=httpx.MockTransport(handler))
        try:
            for language in ["en-IN", "hi-IN", "hi-en", "en-IN"]:
                await getattr(client, method)("same cache test text", language=language)
        finally:
            await client.aclose()

    asyncio.run(run())
    assert captured == (
        ["en-IN", "hi-IN", "hi-en", "en-IN"]
        if method == "synthesize_pcm_blocking"
        else ["en-IN", "hi-IN", "hi-en"]
    )


def test_acknowledgement_never_plays_in_wrong_language(db):
    class SlowPlanner:
        model_name = "test-slow-planner"

        async def complete_json(self, **kwargs):
            await asyncio.sleep(0.65)
            return LLMCompletion(
                text=json.dumps(proposal_reply()), model=self.model_name, latency_ms=650
            )

    async def run():
        speech = LanguageSpeech("Please hand it to me")
        service = VoiceTurnService(db, SlowPlanner(), speech)
        service.set_ack(b"HINGLISH_ACK", 16000)
        call = start_call(service.calls)
        chunks = []

        async def sink(chunk):
            chunks.append(chunk)

        response = await service.handle_transcript(
            call.id,
            speech.text,
            detected_language="hi-IN",
            transcript_confidence=None,
            stt_ms=0,
            stt_mode="scripted",
            audio_sink=sink,
        )
        assert not response.ack_played
        assert b"HINGLISH_ACK" not in chunks

    asyncio.run(run())


def test_volunteered_otp_is_not_returned_in_voice_transcript(scripted_llm):
    speech = LanguageSpeech("My OTP is 123456")
    with voice_client(scripted_llm, llm_outcomes=[], speech=speech) as client:
        sid = start_session(client)
        response = client.post(f"/api/sessions/{sid}/voice-turn", files=audio_file())
        assert response.status_code == 200
        assert "123456" not in response.text
        events = client.get(f"/api/sessions/{sid}/events")
        assert "123456" not in events.text
