import asyncio
import base64
import json

import pytest
from test_realtime import proposal_decision
from test_realtime_a1 import HeldPlanner, voice


class PreparingSpeech:
    def __init__(self, *, hold_preparation=False):
        self.gate = asyncio.Lock()
        self.hold_preparation = hold_preparation
        self.preparing = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.ready = asyncio.Event()
        self.prepared = []
        self.active_preparations = 0

    async def stream_pcm(self, text, *, sample_rate=16000, language=None):
        async with self.gate:
            yield b"REPLY"

    async def synthesize_pcm(self, text, *, sample_rate=16000, language=None):
        self.active_preparations += 1
        try:
            async with self.gate:
                self.preparing.set()
                if self.hold_preparation:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.cancelled.set()
                self.prepared.append((sample_rate, language))
                if len(self.prepared) == 3:
                    self.ready.set()
                return f"ACK:{sample_rate}:{language}".encode()
        finally:
            self.active_preparations -= 1


@pytest.mark.parametrize("rate", [8000, 16000, 48000])
@pytest.mark.parametrize(
    "text,language",
    [("Please hand the parcel to me", "en-IN"), ("मुझे ही पार्सल देना", "hi-IN")],
)
def test_first_turn_can_acknowledge_in_customers_language(scripted_llm, rate, text, language):
    async def run():
        speech = PreparingSpeech()
        planner = HeldPlanner(scripted_llm)
        async with voice(planner, speech=speech) as (ws, _, calls, sid):
            ws.control("config", tts_sample_rate=rate)
            await ws.event("agent_audio_end")
            await asyncio.sleep(0)
            ws.control("scripted_turn", text=text)
            await planner.started.wait()
            chunk = await ws.event("agent_audio_chunk")
            assert base64.b64decode(chunk["chunk_base64"]) == f"ACK:{rate}:{language}".encode()
            await asyncio.sleep(0.05)
            planner.release.set()
            end = await ws.event("agent_audio_end")
            assert end["ack_played"] is True
            assert 500 <= end["timings"]["first_audio_ms"] < 1000
            event = next(e for e in calls.list_events(sid) if e.event_type == "VOICE_TURN")
            timings = json.loads(event.payload_json)
            assert timings["first_response_audio_ms"] >= timings["first_audio_ms"] + 40

    asyncio.run(run())


def test_ack_preparation_releases_shared_speech_slot_for_customer_turn(scripted_llm):
    async def run():
        speech = PreparingSpeech(hold_preparation=True)
        async with voice(scripted_llm([proposal_decision()]), speech=speech) as (ws, _, calls, sid):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_end")
            await speech.preparing.wait()
            ws.control("scripted_turn", text="Please hand the parcel to me")
            await ws.event("turn")
            await ws.event("agent_audio_chunk")
            assert speech.cancelled.is_set()
            end = await ws.event("agent_audio_end")
            assert end["audio_error"] is None
            event = next(e for e in calls.list_events(sid) if e.event_type == "VOICE_TURN")
            timings = json.loads(event.payload_json)
            assert not timings["ack_played"]
            assert timings["first_response_audio_ms"] == timings["first_audio_ms"]

    asyncio.run(run())


def test_cancelled_ack_preparation_can_resume_after_reply(scripted_llm):
    async def run():
        speech = PreparingSpeech(hold_preparation=True)
        async with voice(scripted_llm([proposal_decision()]), speech=speech) as (ws, _, _, _):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_end")
            await speech.preparing.wait()
            speech.hold_preparation = False
            ws.control("scripted_turn", text="Please hand the parcel to me")
            await ws.event("agent_audio_end")
            await asyncio.wait_for(speech.ready.wait(), 1)
            assert set(speech.prepared) == {(16000, language) for language in ("hi-en", "en-IN", "hi-IN")}

    asyncio.run(run())


def test_disconnect_cleans_up_active_and_queued_ack_preparation(scripted_llm):
    async def run():
        speech = PreparingSpeech(hold_preparation=True)
        async with voice(scripted_llm([]), speech=speech) as (ws, _, _, _):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_end")
            await speech.preparing.wait()
            await asyncio.sleep(0)
            assert speech.active_preparations == 3
        assert speech.active_preparations == 0
        assert not speech.gate.locked()

    asyncio.run(run())
