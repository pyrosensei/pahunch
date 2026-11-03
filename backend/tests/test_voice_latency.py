"""Silent planner waits: no acknowledgement audio or synthesis warmups."""
import asyncio
import base64
import json

import pytest
from test_realtime import proposal_decision
from test_realtime_a1 import HeldPlanner, voice


class PreparingSpeech:
    def __init__(self):
        self.prepared = []

    async def stream_pcm(self, text, *, sample_rate=16000, language=None):
        yield b"REPLY"

    async def synthesize_pcm(self, text, *, sample_rate=16000, language=None):
        self.prepared.append(text)
        raise AssertionError("Acknowledgement synthesis must not run")


@pytest.mark.parametrize("rate", [8000, 16000, 48000])
@pytest.mark.parametrize("text", ["Please hand the parcel to me", "मुझे ही पार्सल देना"])
def test_slow_planner_emits_no_filler_and_never_warms_ack(scripted_llm, rate, text):
    async def run():
        speech = PreparingSpeech()
        planner = HeldPlanner(scripted_llm)
        async with voice(planner, speech=speech) as (ws, _, calls, sid):
            ws.control("config", tts_sample_rate=rate)
            await ws.event("agent_audio_end")
            ws.control("scripted_turn", text=text)
            await planner.started.wait()
            before = len(ws.history)
            await asyncio.sleep(0.65)
            assert not any(e["type"] == "agent_audio_chunk" for e in ws.history[before:])
            assert speech.prepared == []
            planner.release.set()
            chunk = await ws.event("agent_audio_chunk")
            assert base64.b64decode(chunk["chunk_base64"]) == b"REPLY"
            end = await ws.event("agent_audio_end")
            assert end["ack_played"] is False
            event = next(e for e in calls.list_events(sid) if e.event_type == "VOICE_TURN")
            timing = json.loads(event.payload_json)
            assert timing["first_response_audio_ms"] == timing["first_audio_ms"]
    asyncio.run(run())


def test_no_warmup_after_replies(scripted_llm):
    async def run():
        speech = PreparingSpeech()
        async with voice(scripted_llm([proposal_decision()]), speech=speech) as (ws, _, _, _):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_end")
            ws.control("scripted_turn", text="Please hand the parcel to me")
            await ws.event("agent_audio_end")
            await asyncio.sleep(0)
            assert speech.prepared == []
    asyncio.run(run())
