import asyncio
import base64
import json
from contextlib import asynccontextmanager
from time import perf_counter

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.websockets import WebSocketDisconnect
from test_realtime import (
    FAST_CONFIG,
    ScriptedStream,
    StreamingSpeech,
    loud,
    proposal_decision,
    realtime_client,
    silence,
    start_session,
)

from app.calls import CallService
from app.realtime import handle_voice_socket
from app.seed import seed_database
from app.streaming_stt import StreamingTranscript


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.history = []

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, event):
        self.history.append(event)
        await self.outgoing.put(event)

    async def close(self, code):
        await self.outgoing.put({"type": "closed", "code": code})

    def control(self, kind, **fields):
        self.incoming.put_nowait(
            {"type": "websocket.receive", "text": json.dumps({"type": kind, **fields})}
        )

    def audio(self, frame):
        self.incoming.put_nowait({"type": "websocket.receive", "bytes": frame})

    def utterance(self):
        for frame in [loud()] * 3 + [silence()] * 3:
            self.audio(frame)

    async def event(self, kind, timeout=1):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.outgoing.get()
                if event["type"] == kind:
                    return event


@asynccontextmanager
async def voice(llm, *, speech=None, streaming=None, config=FAST_CONFIG):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db)
        calls = CallService(db)
        call = calls.create_call(1042)
        calls.connect(call.id)
        calls.confirm_context(call.id)
        socket = Socket()
        task = asyncio.create_task(
            handle_voice_socket(
                socket,
                call.id,
                db,
                llm,
                speech or StreamingSpeech([b"\x01\x02"]),
                config,
                streaming=streaming,
            )
        )
        await socket.event("ready")
        try:
            yield socket, task, calls, call.id
        finally:
            socket.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            try:
                await asyncio.wait_for(task, 1)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    engine.dispose()


class HeldPlanner:
    model_name = "held-test-planner"

    def __init__(self, scripted_llm):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.delegate = scripted_llm([proposal_decision()])

    async def complete_json(self, **kwargs):
        self.started.set()
        try:
            await self.release.wait()
            return await self.delegate.complete_json(**kwargs)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def test_duplicate_socket_is_rejected_and_owner_can_reconnect(scripted_llm):
    with realtime_client(scripted_llm, llm_outcomes=[], speech=StreamingSpeech([])) as client:
        session_id = start_session(client)
        path = f"/ws/sessions/{session_id}/voice"
        with client.websocket_connect(path) as first:
            assert first.receive_json()["type"] == "ready"
            with client.websocket_connect(path) as duplicate:
                error = duplicate.receive_json()
                assert error["type"] == "error"
                with pytest.raises(WebSocketDisconnect) as raised:
                    duplicate.receive_json()
                assert raised.value.code == 4409
            first.send_json({"type": "bogus"})
            assert first.receive_json()["detail"] == "Unknown control message"
        with client.websocket_connect(path) as reconnect:
            assert reconnect.receive_json()["type"] == "ready"
        events = client.get(f"/api/sessions/{session_id}/events").json()
        assert sum(e["event_type"] == "REALTIME_DUPLICATE_REJECTED" for e in events) == 1
        assert sum(e["event_type"] == "REALTIME_CONNECTED" for e in events) == 2


def test_reader_handles_controls_and_drops_frames_during_planning(scripted_llm):
    async def run():
        llm = HeldPlanner(scripted_llm)
        async with voice(llm) as (ws, _, calls, session_id):
            ws.control("start")
            await ws.event("listening")
            ws.utterance()
            await asyncio.wait_for(llm.started.wait(), 1)
            ws.utterance()
            started = perf_counter()
            ws.control("bogus")
            assert (await ws.event("error", timeout=0.3))["detail"] == "Unknown control message"
            measured_ms = (perf_counter() - started) * 1000
            llm.release.set()
            end = await ws.event("agent_audio_end")
            ws.control("start")
            await ws.event("listening")
            ws.control("bogus")
            await ws.event("error")
            calls.db.expire_all()
            assert calls.get_call(session_id).turn_count == 1
            payloads = [
                json.loads(e.payload_json)
                for e in calls.list_events(session_id)
                if e.event_type == "REALTIME_FRAMES_DROPPED"
            ]
            assert sum(p["frames"] for p in payloads) == 6
            print(f"synthetic blocked-planner control response: {measured_ms:.2f} ms")
            print(f"mock-provider server first_audio_ms: {end['timings']['first_audio_ms']}")

    asyncio.run(run())


@pytest.mark.parametrize("ack_wait", [False, True])
def test_reset_cancels_planner_including_ack_wait(scripted_llm, ack_wait):
    async def run():
        llm = HeldPlanner(scripted_llm)
        async with voice(llm) as (ws, _, calls, session_id):
            if ack_wait:
                ws.control("config", tts_sample_rate=16000)
                await ws.event("agent_audio_end")
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            await asyncio.wait_for(llm.started.wait(), 1)
            ws.control("reset")
            ws.control("bogus")
            await ws.event("error", timeout=0.3)
            await asyncio.wait_for(llm.cancelled.wait(), 0.3)
            assert calls.get_call(session_id).turn_count == 0
            assert any(
                e.event_type == "REALTIME_OUTPUT_CANCELLED" for e in calls.list_events(session_id)
            )

    asyncio.run(run())


def test_disconnect_awaits_planner_and_stream_cleanup(scripted_llm):
    async def run():
        llm = HeldPlanner(scripted_llm)
        stream = ScriptedStream()
        async with voice(llm, streaming=stream) as (ws, task, _, _):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_end")
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            await asyncio.wait_for(llm.started.wait(), 1)
            ws.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 0.3)
            assert llm.cancelled.is_set()
            assert stream.closed == 1

    asyncio.run(run())


class HeldSender(ScriptedStream):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.send_cancelled = asyncio.Event()

    async def send_audio(self, frame):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.send_cancelled.set()


def test_stalled_stt_does_not_block_reader_and_overflow_uses_rest(scripted_llm):
    async def run():
        stream = HeldSender()
        speech = StreamingSpeech([b"\x01\x02"])
        async with voice(scripted_llm([proposal_decision()]), speech=speech, streaming=stream) as (
            ws,
            _,
            calls,
            session_id,
        ):
            ws.control("start")
            await ws.event("listening")
            ws.audio(loud())
            await asyncio.wait_for(stream.started.wait(), 1)
            ws.control("bogus")
            await ws.event("error", timeout=0.3)
            for _ in range(30):
                ws.audio(loud())
            ws.control("stop")
            turn = await ws.event("turn")
            assert turn["stt_fallback_reason"] == "QUEUE_OVERFLOW"
            await ws.event("agent_audio_end")
            assert stream.send_cancelled.is_set()
            payloads = [
                json.loads(e.payload_json)
                for e in calls.list_events(session_id)
                if e.event_type == "STREAMING_STT_FAILED"
            ]
            assert any(p["phase"] == "queue" and p["queued_bytes"] <= 32000 for p in payloads)

    asyncio.run(run())


class HeldSpeech(StreamingSpeech):
    def __init__(self):
        super().__init__([])
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def stream_pcm(self, text, *, sample_rate=16000, language=None):
        self.calls += 1
        yield b"\x01\x02"
        if self.calls == 1:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                yield b"LATE"


def test_barge_in_invalidates_late_audio_and_next_generation_still_works(scripted_llm):
    async def run():
        speech = HeldSpeech()
        llm = scripted_llm([proposal_decision(), proposal_decision()])
        async with voice(llm, speech=speech) as (ws, _, calls, session_id):
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            chunk = await ws.event("agent_audio_chunk")
            generation = chunk["generation_id"]
            ws.audio(loud())
            ws.audio(loud())
            stopped = await ws.event("stop_speaking", timeout=0.3)
            assert stopped["generation_id"] == generation
            await asyncio.wait_for(speech.cancelled.wait(), 0.3)
            ws.control("scripted_turn", text="direct handoff chahiye")
            next_chunk = await ws.event("agent_audio_chunk")
            assert next_chunk["generation_id"] > generation
            assert base64.b64decode(next_chunk["chunk_base64"]) != b"LATE"
            end = await ws.event("agent_audio_end")
            assert end["generation_id"] == next_chunk["generation_id"]
            calls.db.expire_all()
            assert calls.get_call(session_id).turn_count == 2

    asyncio.run(run())


def test_early_playback_done_and_start_cannot_reset_active_generation(scripted_llm):
    async def run():
        speech = HeldSpeech()
        async with voice(scripted_llm([proposal_decision()]), speech=speech) as (ws, _, calls, sid):
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            chunk = await ws.event("agent_audio_chunk")
            gen = chunk["generation_id"]
            ws.control("playback_done", generation_id=gen)
            ws.control("start")
            ws.control("bogus")
            await ws.event("error", timeout=0.3)
            assert not speech.cancelled.is_set()
            speech.release.set()
            await ws.event("agent_audio_end")
            ws.control("playback_done", generation_id=gen - 1)
            ws.control("playback_done", generation_id=gen)
            await ws.event("listening")
            ws.audio(loud())
            assert (await ws.event("speech_start"))["type"] == "speech_start"
            assert not any(e.event_type == "BARGE_IN" for e in calls.list_events(sid))

    asyncio.run(run())


def test_greeting_cancelled_on_disconnect_without_late_output(scripted_llm):
    async def run():
        speech = HeldSpeech()
        async with voice(scripted_llm([]), speech=speech) as (ws, task, _, _):
            ws.control("config", tts_sample_rate=16000)
            await ws.event("agent_audio_chunk")
            ws.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 0.3)
            assert speech.cancelled.is_set()
            assert ws.outgoing.empty()

    asyncio.run(run())


def test_reset_during_stt_connect_can_capture_again(scripted_llm):
    async def run():
        gate = asyncio.Event()

        class ConnectingStream(ScriptedStream):
            async def connect(self):
                self.connect_attempts += 1
                await gate.wait()

        stream = ConnectingStream()
        async with voice(scripted_llm([proposal_decision()]), streaming=stream) as (ws, _, _, _):
            ws.control("start")
            await ws.event("listening")
            ws.control("reset")
            ws.control("start")
            await ws.event("listening")
            gate.set()
            ws.utterance()
            assert (await ws.event("turn"))["transcript"] == "6 se 8 aa jaana"
            await ws.event("agent_audio_end")

    asyncio.run(run())


def test_busy_scripted_turn_is_rejected_without_running_second_planner(scripted_llm):
    async def run():
        llm = HeldPlanner(scripted_llm)
        async with voice(llm) as (ws, _, _, _):
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            await asyncio.wait_for(llm.started.wait(), 1)
            ws.control("scripted_turn", text="another instruction")
            assert (await ws.event("error"))["detail"] == "A voice turn is already in progress"
            llm.release.set()
            await ws.event("agent_audio_end")
            assert len(llm.delegate.requests) == 1

    asyncio.run(run())


def test_playback_tracking_waits_for_current_generation_ack(scripted_llm):
    async def run():
        async with voice(scripted_llm([])) as (ws, _, calls, session_id):
            ws.control("config", tts_sample_rate=16000, playback_tracking=True)
            ws.control("start")
            end = await ws.event("agent_audio_end")
            gen = end["generation_id"]
            ws.control("start")
            ws.control("playback_done", generation_id=gen - 1)
            ws.control("bogus")
            await ws.event("error")
            assert not any(e["type"] == "listening" for e in ws.history)
            ws.control("playback_done", generation_id=gen)
            await ws.event("listening")
            ws.audio(loud())
            await ws.event("speech_start")
            assert not any(e.event_type == "BARGE_IN" for e in calls.list_events(session_id))

    asyncio.run(run())


def test_stream_close_failure_is_audited_without_hiding_disconnect(scripted_llm):
    async def run():
        class FailedClose(ScriptedStream):
            async def close(self):
                raise OSError("test transport close failure")

        async with voice(scripted_llm([]), streaming=FailedClose()) as (ws, task, calls, sid):
            ws.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 1)
            events = calls.list_events(sid)
            assert any(e.event_type == "REALTIME_DISCONNECTED" for e in events)
            assert any(
                e.event_type == "STREAMING_STT_FAILED"
                and json.loads(e.payload_json)["phase"] == "close"
                for e in events
            )

    asyncio.run(run())


def test_repeated_reset_retains_ownership_until_cancelled_output_finishes(scripted_llm):
    async def run():
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_finished = asyncio.Event()

        class CleaningSpeech(StreamingSpeech):
            async def stream_pcm(self, text, *, sample_rate=16000, language=None):
                yield b"\x01\x02"
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    cleanup_finished.set()
                    raise

        async with voice(scripted_llm([proposal_decision()]), speech=CleaningSpeech([])) as (
            ws,
            task,
            _,
            _,
        ):
            ws.control("scripted_turn", text="6 se 8 aa jaana")
            await ws.event("agent_audio_chunk")
            ws.control("reset")
            await asyncio.wait_for(cleanup_started.wait(), 1)
            ws.control("scripted_turn", text="another instruction")
            ws.control("reset")
            ws.control("bogus")
            await ws.event("error")
            ws.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.sleep(0)
            assert not cleanup_finished.is_set()
            assert not task.done()
            cleanup_release.set()
            await asyncio.wait_for(task, 1)
            assert cleanup_finished.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("rest_fallback", [False, True])
def test_turn_timings_include_stream_finish_wait(scripted_llm, rest_fallback):
    async def run():
        class DelayedFinish(ScriptedStream):
            waited_ms = 0

            async def finish_utterance(self):
                started = perf_counter()
                await asyncio.sleep(0.04)
                self.waited_ms = (perf_counter() - started) * 1000
                return None if rest_fallback else StreamingTranscript(text="6 se 8 aa jaana")

        stream = DelayedFinish()
        async with voice(scripted_llm([proposal_decision()]), streaming=stream) as (ws, _, _, _):
            ws.control("start")
            await ws.event("listening")
            ws.utterance()
            end = await ws.event("agent_audio_end")
            assert end["timings"]["first_audio_ms"] >= stream.waited_ms - 2
            assert end["timings"]["total_ms"] >= stream.waited_ms - 2

    asyncio.run(run())
