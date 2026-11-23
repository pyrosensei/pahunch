"""Speech-start timing and completed-stream cache regressions."""

import asyncio
import base64
import json

import httpx
import pytest
from test_gnani import make_client
from test_realtime import proposal_decision
from test_realtime_a1 import voice

from app.gnani import PCM_CACHE_BYTES, PCM_CACHE_ENTRIES, GnaniResponseError


def sse(audio):
    return "data: " + json.dumps({"audio": base64.b64encode(audio).decode()}) + "\n\n"


def test_successful_blocking_fallback_populates_shared_pcm_cache():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, content=b"\x01\x02" * 5000)

    async def run():
        client = make_client(handler)
        try:
            original = await client.synthesize_pcm_blocking("Validated fallback")
            assert await client.synthesize_pcm_blocking("Validated fallback") == original
            replay = b"".join([chunk async for chunk in client.stream_pcm("Validated fallback")])
            assert replay == original
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


def test_completed_stream_is_replayed_without_provider_request():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=sse(b"\x01\x02" * 5000), headers={"content-type": "text/event-stream"}
        )

    async def run():
        client = make_client(handler)
        try:
            first = b"".join(
                [chunk async for chunk in client.stream_pcm("Validated reply", language="hi-en")]
            )
            cached = b"".join(
                [chunk async for chunk in client.stream_pcm("Validated reply", language="hi-en")]
            )
            assert first == cached and len(requests) == 1
            await client.synthesize_pcm("Validated reply", language="hi-en")
            assert len(requests) == 1
            await client.synthesize_pcm("Validated reply", sample_rate=24000, language="hi-en")
            await client.synthesize_pcm("Validated reply", language="en-IN")
            assert len(requests) == 3
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["cancelled", "provider_error", "invalid_audio"])
def test_partial_or_failed_stream_never_populates_cache(ending):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        extra = (
            'data: {"type":"error"}\n\n'
            if ending == "provider_error"
            else 'data: {"audio":"!!!"}\n\n'
        )
        return httpx.Response(200, text=sse(b"\x01\x02") + extra)

    async def run():
        client = make_client(handler)
        try:
            stream = client.stream_pcm("Reply")
            assert await anext(stream) == b"\x01\x02"
            if ending == "cancelled":
                await stream.aclose()
            else:
                with pytest.raises(GnaniResponseError):
                    await anext(stream)
            assert not client._pcm_cache
            stream = client.stream_pcm("Reply")
            await anext(stream)
            await stream.aclose()
            assert count == 2
        finally:
            await client.aclose()

    asyncio.run(run())


def test_pcm_cache_is_bounded_and_evicts_least_recently_used():
    client = make_client(lambda request: httpx.Response(200))
    for number in range(PCM_CACHE_ENTRIES):
        client._remember_pcm((str(number), 16000, "hi-en"), b"\x00\x00")
    client._pcm_cache.move_to_end(("0", 16000, "hi-en"))
    client._remember_pcm(("new", 16000, "hi-en"), b"\x01\x02")
    assert ("0", 16000, "hi-en") in client._pcm_cache
    assert ("1", 16000, "hi-en") not in client._pcm_cache
    for number in range(12):
        client._remember_pcm((f"large-{number}", 16000, "hi-en"), b"\0" * (1024 * 1024))
    assert client._pcm_cache_bytes <= PCM_CACHE_BYTES
    assert client._pcm_cache_bytes == sum(map(len, client._pcm_cache.values()))
    assert len(client._pcm_cache) <= PCM_CACHE_ENTRIES


def test_playback_start_diagnostics_accept_only_current_generation_once(scripted_llm):
    async def run():
        async with voice(scripted_llm([proposal_decision()])) as (ws, _, calls, sid):
            ws.control("config", playback_tracking=True)
            end = await ws.event("agent_audio_end")
            generation = end["generation_id"]
            ws.control(
                "playback_started", generation_id=generation + 1, client_text_to_playback_ms=100
            )
            ws.control(
                "playback_started",
                generation_id=generation,
                client_text_to_playback_ms=1250,
                client_audio_buffer_ms=130,
            )
            ws.control(
                "playback_started", generation_id=generation, client_text_to_playback_ms=9999
            )
            ws.control("config", tts_sample_rate=16000)
            await ws.event("config_ack")
            events = [
                json.loads(event.payload_json)
                for event in calls.list_events(sid)
                if event.event_type == "BROWSER_PLAYBACK_STARTED"
            ]
            assert len(events) == 1
            assert events[0]["client_text_to_playback_ms"] == 1250
            assert events[0]["client_audio_buffer_ms"] == 130

    asyncio.run(run())


def test_voice_timings_separate_text_preparation_from_first_tts_chunk(scripted_llm):
    class DelayedSpeech:
        async def stream_pcm(self, text, **kwargs):
            await asyncio.sleep(0.025)
            yield b"\x01\x02" * 128
            await asyncio.sleep(0.025)
            yield b"\x03\x04" * 128

    async def run():
        async with voice(scripted_llm([proposal_decision()]), speech=DelayedSpeech()) as (
            ws,
            _,
            calls,
            sid,
        ):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_end")
            ws.control("scripted_turn", text="Please hand the parcel to me")
            turn = await ws.event("turn")
            assert turn["timings"]["text_ready_ms"] is not None
            end = await ws.event("agent_audio_end")
            timings = end["timings"]
            assert timings["tts_first_chunk_ms"] >= 15
            assert timings["tts_ms"] >= timings["tts_first_chunk_ms"]
            assert timings["first_response_audio_ms"] > timings["text_ready_ms"]
            event = next(item for item in calls.list_events(sid) if item.event_type == "VOICE_TURN")
            assert (
                json.loads(event.payload_json)["tts_first_chunk_ms"]
                == timings["tts_first_chunk_ms"]
            )

    asyncio.run(run())
