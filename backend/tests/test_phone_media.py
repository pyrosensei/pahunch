import asyncio
import base64
import json

import pytest
from test_dialer import engine as dialer_engine
from test_telephony import phone as phone_fixture

from app.errors import DomainError

engine = dialer_engine
phone = phone_fixture


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False

    def event(self, event):
        self.messages.put_nowait({"type": "websocket.receive", "text": json.dumps(event)})

    async def receive(self):
        return await self.messages.get()

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self, code=1000):
        self.closed = True


def test_phone_socket_reuses_config_frames_and_final_playback_tracking():
    from app.phone_media import PhoneSocket

    async def run():
        ws = Socket()
        adapter = PhoneSocket(ws, "MZtest", lambda *args: None)
        config = json.loads((await adapter.receive())["text"])
        assert config["tts_sample_rate"] == 8000 and config["playback_tracking"]
        assert json.loads((await adapter.receive())["text"])["type"] == "start"
        await adapter.send_json(
            {
                "type": "agent_audio_chunk",
                "generation_id": 1,
                "sample_rate": 8000,
                "chunk_base64": base64.b64encode(b"\0\0\x08\0").decode(),
            }
        )
        assert base64.b64decode(ws.sent[-1]["media"]["payload"]) == b"\xff\xfe"
        await adapter.send_json({"type": "agent_audio_end", "generation_id": 1})
        mark = ws.sent[-1]["mark"]["name"]
        ws.event({"event": "mark", "streamSid": "MZtest", "mark": {"name": mark}})
        assert json.loads((await adapter.receive())["text"]) == {
            "type": "playback_done",
            "generation_id": 1,
        }
        assert json.loads((await adapter.receive())["text"])["type"] == "start"

    asyncio.run(run())


def test_phone_clear_ignores_flushed_mark_and_reframes_inbound_mulaw():
    from app.phone_media import PhoneSocket

    async def run():
        ws = Socket()
        adapter = PhoneSocket(ws, "MZtest", lambda *args: None)
        await adapter.receive()
        await adapter.receive()
        await adapter.send_json({"type": "agent_audio_end", "generation_id": 1})
        old = ws.sent[-1]["mark"]["name"]
        await adapter.send_json({"type": "stop_speaking", "generation_id": 1})
        assert ws.sent[-1]["event"] == "clear"
        ws.event({"event": "mark", "streamSid": "MZtest", "mark": {"name": old}})
        for index in range(4):
            ws.event(
                {
                    "event": "media",
                    "streamSid": "MZtest",
                    "sequenceNumber": str(index + 1),
                    "media": {
                        "track": "inbound",
                        "payload": base64.b64encode(b"\xff" * 160).decode(),
                    },
                }
            )
        message = await adapter.receive()
        assert message["bytes"] == b"\0" * 1024
        assert len(adapter.input_buffer) < 1024

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        {"streamSid": "wrong"},
        {"media": {"track": "inbound", "payload": "!!"}},
        {"media": {"track": "inbound", "payload": "A" * 12000}},
        {"media": None},
        {"sequenceNumber": None},
    ],
)
def test_phone_media_rejects_wrong_stream_or_malformed_audio(change):
    from app.phone_media import PhoneSocket

    async def run():
        ws = Socket()
        adapter = PhoneSocket(ws, "MZtest", lambda *args: None)
        await adapter.receive()
        await adapter.receive()
        event = {
            "event": "media",
            "streamSid": "MZtest",
            "sequenceNumber": "1",
            "media": {"track": "inbound", "payload": "/w=="},
            **change,
        }
        ws.event(event)
        with pytest.raises(DomainError):
            await adapter.receive()

    asyncio.run(run())


@pytest.fixture
def phone_app(phone, scripted_llm):
    from dataclasses import replace

    from fastapi.testclient import TestClient
    from test_realtime import FAST_CONFIG, ScriptedStream, valid_proposal

    from app.main import create_app
    from app.streaming_stt import GnaniStreamingSTTClient, StreamingTranscript

    class NativeSpeech:
        def __init__(self):
            self.calls = []

        async def stream_pcm(self, text, **kwargs):
            self.calls.append(kwargs)
            yield b"\xff" * 160

        async def synthesize_pcm_blocking(self, text, **kwargs):
            return b"\xff" * 160

    transport, _, sid, _ = phone
    transport.dispatch_if_approved(sid)
    speech = NativeSpeech()

    class FramedStream(ScriptedStream):
        def __init__(self, outcomes):
            super().__init__(outcomes)
            self.framer = GnaniStreamingSTTClient(api_key="synthetic-test-key", sample_rate=8000)
            captured = self.frames

            class Wire:
                async def send(self, frame):
                    captured.append(frame)

            self.framer._ws = Wire()

        async def send_audio(self, pcm):
            await self.framer.send_audio(pcm)

    stream = FramedStream(
        [
            StreamingTranscript("Please hand it to me", "en-IN", 1),
            StreamingTranscript("yes", "en-IN", 1),
        ]
    )
    llm = scripted_llm(
        [
            {
                "action": "PROPOSE_PLAN",
                "message": "{plan_summary} {confirmation_question}",
                "proposal": valid_proposal(),
                "escalation_reason": None,
                "confidence": 0.95,
            }
        ]
    )
    app = create_app(
        init_database=False,
        telephony=transport,
        llm_client=llm,
        speech_client=speech,
        streaming_stt_client=stream,
        realtime_config=replace(FAST_CONFIG, sample_rate=8000),
    )
    with TestClient(app) as client:
        yield client, speech, stream


def start_event(cfg, *, call_sid="CA" + "1" * 32, rate=8000):
    stream = "MZ" + "4" * 32
    return {
        "event": "start",
        "streamSid": stream,
        "sequenceNumber": "1",
        "start": {
            "accountSid": cfg.twilio_account_sid,
            "callSid": call_sid,
            "streamSid": stream,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": rate, "channels": 1},
        },
    }


def socket_headers(phone):
    from app.telephony import signature

    transport, cfg, sid, _ = phone
    url = transport.url(f"/twilio/media/{sid}").replace("https://", "wss://", 1)
    return {"X-Twilio-Signature": signature(cfg.twilio_auth_token, url, {})}


def test_phone_socket_requires_signed_handshake_and_explicit_consent(phone, phone_app):
    from starlette.websockets import WebSocketDisconnect

    client = phone_app[0]
    with (
        client.websocket_connect(f"/twilio/media/{phone[2]}") as ws,
        pytest.raises(WebSocketDisconnect),
    ):
        ws.receive_json()
    with (
        client.websocket_connect(f"/twilio/media/{phone[2]}", headers=socket_headers(phone)) as ws,
        pytest.raises(WebSocketDisconnect),
    ):
        ws.receive_json()


@pytest.mark.parametrize("change", [{"call_sid": "CA" + "9" * 32}, {"rate": 16000}])
def test_phone_stream_start_checks_call_binding_and_format(phone, phone_app, change):
    from starlette.websockets import WebSocketDisconnect
    from test_telephony import signed_post

    client = phone_app[0]
    signed_post(phone, client, "answer")
    signed_post(phone, client, "consent", Digits="1")
    with client.websocket_connect(f"/twilio/media/{phone[2]}", headers=socket_headers(phone)) as ws:
        ws.send_json(start_event(phone[1], **change))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_mock_phone_audio_negotiates_then_commits_only_after_yes(phone, phone_app):
    from sqlmodel import Session
    from test_telephony import signed_post

    from app.models import CallSession, CallState
    from app.phone_audio import encode_mulaw

    client, speech, stream = phone_app
    signed_post(phone, client, "answer")
    signed_post(phone, client, "consent", Digits="1")
    sid = phone[2]
    event = start_event(phone[1])
    sequence = 2

    def mark(ws):
        for _ in range(30):
            message = ws.receive_json()
            if message["event"] == "mark":
                return message
        raise AssertionError("No playback mark")

    def utterance(ws):
        nonlocal sequence
        for value, count in [(3000, 512 * 5), (0, 512 * 6)]:
            data = encode_mulaw((value.to_bytes(2, "little", signed=True)) * count)
            for offset in range(0, len(data), 160):
                ws.send_json(
                    {
                        "event": "media",
                        "streamSid": event["streamSid"],
                        "sequenceNumber": str(sequence),
                        "media": {
                            "track": "inbound",
                            "payload": base64.b64encode(data[offset : offset + 160]).decode(),
                        },
                    }
                )
                sequence += 1

    with client.websocket_connect(f"/twilio/media/{sid}", headers=socket_headers(phone)) as ws:
        ws.send_json(event)
        ws.send_json(mark(ws))
        utterance(ws)
        first = mark(ws)
        with Session(phone[0].engine) as db:
            call = db.get(CallSession, sid)
            assert call.state == CallState.PENDING_CONFIRMATION and not call.customer_confirmed
        ws.send_json(first)
        utterance(ws)
        final = mark(ws)
        with Session(phone[0].engine) as db:
            assert db.get(CallSession, sid).state == CallState.COMMITTED
        ws.send_json(final)
    assert all(c["sample_rate"] == 8000 and c["container"] == "mulaw" for c in speech.calls)
    assert stream.frames and all(len(frame) == 1024 for frame in stream.frames)
