import asyncio
import json

from test_realtime import (
    ScriptedStream,
    StreamingSpeech,
    proposal_decision,
    realtime_client,
    start_session,
)
from test_realtime_a1 import voice

from app.realtime import RealtimeConfig
from scripts.vad_probe import frame


async def pcm(ws, rms, ms):
    for i in range(ms // 20):
        ws.audio(frame(rms, index=i))
        await asyncio.sleep(0.001)


def test_quiet_speech_reaches_planner_with_pre_roll(scripted_llm):
    async def run():
        stream = ScriptedStream()
        async with voice(
            scripted_llm([proposal_decision()]), streaming=stream, config=RealtimeConfig()
        ) as (ws, _, calls, sid):
            ws.control("start")
            await ws.event("listening")
            await pcm(ws, 20, 500)
            assert stream.frames == []
            await pcm(ws, 220, 800)
            await pcm(ws, 20, 600)
            assert (await ws.event("turn"))["transcript"] == "6 se 8 aa jaana"
            await ws.event("agent_audio_end")
            assert stream.frames[0] == frame(20) * 10 + frame(220) * 5
            events = calls.list_events(sid)
            assert any(e.event_type == "VAD_CALIBRATED" for e in events)
            assert any(e.event_type == "VAD_UTTERANCE_ENDED" for e in events)

    asyncio.run(run())


def test_sustained_barge_in_ignores_spikes_and_preserves_onset(scripted_llm):
    async def run():
        config = RealtimeConfig(adaptive_vad=False)
        async with voice(scripted_llm([proposal_decision()]), config=config) as (ws, _, calls, sid):
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            audio = await ws.event("agent_audio_chunk")
            await ws.event("agent_audio_end")
            ws.audio(frame(3000))
            await pcm(ws, 20, 200)
            ws.control("bogus")
            await ws.event("error")
            assert not any(e["type"] == "stop_speaking" for e in ws.history)
            await pcm(ws, 3000, 160)
            stop = await ws.event("stop_speaking")
            assert stop["generation_id"] == audio["generation_id"]
            assert sum(e.event_type == "BARGE_IN" for e in calls.list_events(sid)) == 1

    asyncio.run(run())


def test_rejected_short_utterance_resets_without_planner(scripted_llm):
    async def run():
        async with voice(scripted_llm([]), config=RealtimeConfig()) as (ws, _, calls, sid):
            ws.control("config", tts_sample_rate=16000, audio_feedback=True)
            await ws.event("agent_audio_end")
            ws.control("start")
            await ws.event("listening")
            await pcm(ws, 20, 500)
            await pcm(ws, 1600, 120)
            await pcm(ws, 20, 600)
            assert (await ws.event("no_speech"))["reason"] == "SPEECH_TOO_SHORT"
            ws.control("bogus")
            await ws.event("error")
            assert not any(e["type"] == "turn" for e in ws.history)
            assert any(e.event_type == "VAD_SHORT_UTTERANCE" for e in calls.list_events(sid))

    asyncio.run(run())


def test_no_audio_and_no_speech_feedback_are_visible_and_not_repeated(scripted_llm):
    async def run():
        config = RealtimeConfig(no_speech_ms=60)
        async with voice(scripted_llm([]), config=config) as (ws, _, calls, sid):
            ws.control("config", tts_sample_rate=16000, audio_feedback=True)
            await ws.event("agent_audio_end")
            ws.control("start")
            await ws.event("listening")
            hint = await ws.event("no_speech")
            assert hint["reason"] == "NO_AUDIO_FRAMES"
            level = await ws.event("audio_level")
            assert "threshold" in level and "noise_floor" in level
            await asyncio.sleep(0.3)
            assert sum(e.event_type == "VAD_NO_SPEECH" for e in calls.list_events(sid)) == 1
            ws.control("reset")
            ws.control("start")
            await ws.event("listening")
            await pcm(ws, 20, 500)
            hint = await ws.event("no_speech")
            assert hint["reason"] == "NO_SPEECH_DETECTED"
            assert not any(e["type"] == "turn" for e in ws.history)
            payloads = [
                json.loads(e.payload_json)
                for e in calls.list_events(sid)
                if e.event_type == "VAD_NO_SPEECH"
            ]
            assert len(payloads) == 2

    asyncio.run(run())


def test_feedback_task_stops_on_disconnect(scripted_llm):
    async def run():
        async with voice(scripted_llm([]), config=RealtimeConfig(no_speech_ms=60)) as (
            ws,
            task,
            _,
            _,
        ):
            ws.control("config", tts_sample_rate=16000, audio_feedback=True)
            await ws.event("agent_audio_end")
            ws.control("start")
            await ws.event("listening")
            ws.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 1)
            count = len(ws.history)
            await asyncio.sleep(0.3)
            assert len(ws.history) == count

    asyncio.run(run())


def test_stopped_audio_stream_has_distinct_hint(scripted_llm):
    async def run():
        config = RealtimeConfig(no_speech_ms=550)
        async with voice(scripted_llm([]), config=config) as (ws, _, _, _):
            ws.control("config", tts_sample_rate=16000, audio_feedback=True)
            await ws.event("agent_audio_end")
            ws.control("start")
            await ws.event("listening")
            await pcm(ws, 20, 300)
            hint = await ws.event("no_speech")
            assert hint["reason"] == "AUDIO_STREAM_STOPPED"

    asyncio.run(run())


def test_excessive_noise_has_distinct_hint(scripted_llm):
    async def run():
        config = RealtimeConfig(no_speech_ms=100)
        async with voice(scripted_llm([]), config=config) as (ws, _, _, _):
            ws.control("config", tts_sample_rate=16000, audio_feedback=True)
            await ws.event("agent_audio_end")
            ws.control("start")
            await ws.event("listening")
            await pcm(ws, 10000, 500)
            hint = await ws.event("no_speech")
            assert hint["reason"] == "EXCESSIVE_NOISE"
            assert not any(e["type"] == "speech_start" for e in ws.history)

    asyncio.run(run())


def test_disconnect_waits_for_delayed_stream_close_with_feedback_enabled(scripted_llm):
    class SlowClose(ScriptedStream):
        async def close(self):
            await asyncio.sleep(0.02)
            self.closed += 1

    stream = SlowClose()
    with realtime_client(
        scripted_llm, llm_outcomes=[], speech=StreamingSpeech([]), streaming=stream
    ) as client:
        sid = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{sid}/voice") as ws:
            ws.receive_json()
            ws.send_json({"type": "config", "audio_feedback": True})
            ws.receive_json()
            ws.receive_json()
    assert stream.closed == 1


def test_repeated_barge_in_requires_fresh_sustained_speech(scripted_llm):
    async def run():
        config = RealtimeConfig(adaptive_vad=False)
        async with voice(scripted_llm([proposal_decision()] * 3), config=config) as (
            ws,
            _,
            calls,
            sid,
        ):
            generations = []
            for _ in range(3):
                ws.control("scripted_turn", text="direct handoff chahiye")
                audio = await ws.event("agent_audio_chunk")
                await ws.event("agent_audio_end")
                ws.audio(frame(3000))
                await pcm(ws, 20, 60)
                await pcm(ws, 3000, 160)
                stopped = await ws.event("stop_speaking")
                assert stopped["generation_id"] == audio["generation_id"]
                generations.append(stopped["generation_id"])
                await ws.event("speech_start")
                ws.control("reset")
                ws.control("bogus")
                await ws.event("error")
            assert len(set(generations)) == 3
            assert sum(e.event_type == "BARGE_IN" for e in calls.list_events(sid)) == 3

    asyncio.run(run())
