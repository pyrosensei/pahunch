import base64
import io
import json
import struct
import wave
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.websockets import WebSocketDisconnect

from app.db import get_session
from app.gnani import SpeechAudio, Transcript
from app.main import create_app
from app.realtime import RealtimeConfig, UtteranceBuffer, frame_is_speech, pcm16_to_wav
from app.seed import seed_database
from app.streaming_stt import StreamingSTTError, StreamingTranscript

START = datetime.fromisoformat("2026-11-05T18:00:00+05:30")
END = datetime.fromisoformat("2026-11-05T20:00:00+05:30")
RATE = 16_000

FAST_CONFIG = RealtimeConfig(
    sample_rate=RATE,
    vad_threshold=2_000,
    silence_ms=300,
    min_speech_ms=100,
    max_utterance_ms=5_000,
)


def pcm_frame(amplitude: int, ms: int = 100) -> bytes:
    count = RATE * ms // 1000
    return struct.pack(f"<{count}h", *([amplitude] * count))


def loud() -> bytes:
    return pcm_frame(3_000)


def silence() -> bytes:
    return pcm_frame(0)


def valid_proposal() -> dict:
    return {
        "primary": {
            "action": "HAND_TO_CUSTOMER",
            "window_start": START.isoformat(),
            "window_end": END.isoformat(),
        },
        "fallback": {
            "action": "RETRY_AFTER_DELAY",
            "delay_minutes": 15,
            "max_retries": 1,
        },
    }


def proposal_decision() -> dict:
    return {
        "action": "PROPOSE_PLAN",
        "message": "6 se 8 ka slot available hai. Confirm karein?",
        "proposal": valid_proposal(),
        "escalation_reason": None,
        "confidence": 0.92,
    }


@contextmanager
def realtime_client(
    scripted_llm,
    *,
    llm_outcomes: list[object],
    speech,
    config: RealtimeConfig = FAST_CONFIG,
    streaming=None,
):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session)

    def override_session():
        with Session(engine) as session:
            yield session

    app = create_app(
        init_database=False,
        llm_client=scripted_llm(llm_outcomes),
        speech_client=speech,
        streaming_stt_client=streaming,
        realtime_config=config,
    )
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client


class ScriptedStream:
    def __init__(
        self,
        transcripts: list[object] | None = None,
        *,
        connect_error: str | None = None,
        finish_error: str | None = None,
    ):
        self.transcripts = list(transcripts or [])
        self.connect_error = connect_error
        self.finish_error = finish_error
        self.connect_attempts = 0
        self.began = 0
        self.cancelled = 0
        self.frames: list[bytes] = []
        self.commits = 0
        self.closed = 0

    async def connect(self):
        self.connect_attempts += 1
        if self.connect_error:
            raise StreamingSTTError(self.connect_error)

    async def begin_utterance(self):
        self.began += 1

    async def send_audio(self, pcm: bytes):
        self.frames.append(pcm)

    async def cancel_utterance(self):
        self.cancelled += 1

    async def finish_utterance(self):
        self.commits += 1
        if self.finish_error:
            raise StreamingSTTError(self.finish_error)
        if not self.transcripts:
            return None
        return self.transcripts.pop(0)

    async def close(self):
        self.closed += 1


def start_session(client: TestClient) -> str:
    created = client.post("/api/orders/1042/call")
    assert created.status_code == 201
    session_id = created.json()["id"]
    client.post(f"/api/sessions/{session_id}/connect").raise_for_status()
    client.post(f"/api/sessions/{session_id}/context-confirmation").raise_for_status()
    return session_id


def send_utterance(ws, speech_ms: int = 300, silence_ms: int = 300) -> None:
    for _ in range(speech_ms // 100):
        ws.send_bytes(loud())
    for _ in range(silence_ms // 100):
        ws.send_bytes(silence())


def test_realtime_turn_streams_stt_planner_policy_and_tts(scripted_llm, scripted_speech):
    speech = scripted_speech(
        [
            Transcript(
                text="6 se 8 baje aa jaana",
                language="hi-IN",
                confidence=0.91,
                latency_ms=110,
            )
        ],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=60)],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["session_id"] == session_id
            assert ready["state"] == "DISCOVERY"

            ws.send_json({"type": "start"})
            assert ws.receive_json() == {"type": "listening"}
            send_utterance(ws)

            assert ws.receive_json() == {"type": "speech_start"}
            ended = ws.receive_json()
            assert ended["type"] == "speech_end"
            assert ended["duration_ms"] == 300

            turn = ws.receive_json()
            assert turn["type"] == "turn"
            assert turn["state"] == "PENDING_CONFIRMATION"
            assert turn["transcript"] == "6 se 8 baje aa jaana"
            assert turn["detected_language"] == "hi-IN"
            assert turn["transcript_confidence"] == 0.91
            assert turn["turn"]["verdict"]["valid"] is True
            assert turn["planner"]["action"] == "PROPOSE_PLAN"
            assert turn["timings"]["stt_ms"] == 110

            audio = ws.receive_json()
            assert audio["type"] == "agent_audio"
            assert base64.b64decode(audio["audio_base64"]) == b"AUDIO"
            assert audio["content_type"] == "audio/wav"

        wav = speech.transcribe_calls[0]
        assert wav["filename"] == "audio.wav"
        assert wav["content_type"] == "audio/wav"
        assert wav["bytes"] == 44 + 600 * RATE // 1000 * 2
        assert speech.synthesize_calls == [turn["agent_message"]]

        events = client.get(f"/api/sessions/{session_id}/events").json()
        types = [event["event_type"] for event in events]
        assert "REALTIME_CONNECTED" in types
        assert "VOICE_TURN" in types


def test_push_to_talk_stop_finalizes_the_utterance(scripted_llm, scripted_speech):
    speech = scripted_speech(
        [Transcript(text="theek hai", confidence=0.9, latency_ms=80)],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=40)],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            ws.send_bytes(loud())
            ws.send_bytes(loud())
            ws.send_bytes(loud())
            ws.send_json({"type": "stop"})

            assert ws.receive_json() == {"type": "speech_start"}
            ended = ws.receive_json()
            assert ended["type"] == "speech_end"
            assert ended["duration_ms"] == 300
            turn = ws.receive_json()
            assert turn["type"] == "turn"
            assert turn["transcript"] == "theek hai"
            assert ws.receive_json()["type"] == "agent_audio"


def test_barge_in_stops_agent_audio_and_accepts_confirmation(
    scripted_llm, scripted_speech
):
    speech = scripted_speech(
        [
            Transcript(text="6 se 8 aa jaana", confidence=0.9, latency_ms=90),
            Transcript(
                text="हाँ, ठीक है।",
                language="hi-IN",
                confidence=0.93,
                latency_ms=90,
            ),
        ],
        [
            SpeechAudio(data=b"AUDIO1", content_type="audio/wav", latency_ms=40),
            SpeechAudio(data=b"AUDIO2", content_type="audio/wav", latency_ms=40),
        ],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            assert ws.receive_json()["state"] == "PENDING_CONFIRMATION"
            assert ws.receive_json()["type"] == "agent_audio"

            ws.send_bytes(loud())
            assert ws.receive_json() == {"type": "stop_speaking"}
            assert ws.receive_json() == {"type": "listening"}
            assert ws.receive_json() == {"type": "speech_start"}
            ws.send_bytes(loud())
            ws.send_bytes(loud())
            ws.send_bytes(silence())
            ws.send_bytes(silence())
            ws.send_bytes(silence())
            ws.receive_json()  # speech_end

            confirmed = ws.receive_json()
            assert confirmed["type"] == "turn"
            assert confirmed["state"] == "PENDING_CONFIRMATION"
            assert confirmed["turn"]["customer_confirmed"] is True
            assert confirmed["planner"]["model"] == "rules:explicit-confirmation"
            assert confirmed["planner"]["action"] == "CONFIRM_PLAN"

        events = client.get(f"/api/sessions/{session_id}/events").json()
        types = [event["event_type"] for event in events]
        assert "BARGE_IN" in types
        assert "CONFIRMATION_RULE" in types


def test_turn_in_wrong_call_state_sends_error_event(scripted_llm, scripted_speech):
    speech = scripted_speech([], [])

    with realtime_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client:
        created = client.post("/api/orders/1042/call")
        session_id = created.json()["id"]
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ready = ws.receive_json()
            assert ready["state"] == "CALLING"
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "not available in call state" in error["detail"]
        assert speech.transcribe_calls == []


def test_unknown_session_closes_with_error(scripted_llm, scripted_speech):
    speech = scripted_speech([], [])
    with realtime_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client, client.websocket_connect("/ws/sessions/nope/voice") as ws:
        error = ws.receive_json()
        assert error["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_unknown_and_malformed_control_messages(scripted_llm, scripted_speech):
    speech = scripted_speech([], [])
    with realtime_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "bogus"})
            assert ws.receive_json() == {
                "type": "error",
                "detail": "Unknown control message",
            }
            ws.send_text("not json")
            assert ws.receive_json() == {
                "type": "error",
                "detail": "Invalid control message",
            }


def test_socket_closes_when_voice_is_unconfigured(scripted_llm):
    with realtime_client(
        scripted_llm,
        llm_outcomes=[],
        speech=None,
    ) as client, client.websocket_connect("/ws/sessions/any/voice") as ws:
        error = ws.receive_json()
        assert error == {
            "type": "error",
            "detail": "Voice is not configured on this server",
        }
        with pytest.raises(WebSocketDisconnect) as raised:
            ws.receive_json()
        assert raised.value.code == 1013


def test_pcm16_to_wav_writes_a_valid_header():
    pcm = pcm_frame(1_000, ms=50)
    wav = pcm16_to_wav(pcm, RATE)
    with wave.open(io.BytesIO(wav), "rb") as parsed:
        assert parsed.getnchannels() == 1
        assert parsed.getsampwidth() == 2
        assert parsed.getframerate() == RATE
        assert parsed.readframes(parsed.getnframes()) == pcm


def test_frame_is_speech_uses_rms_threshold():
    assert frame_is_speech(pcm_frame(3_000), 2_000)
    assert not frame_is_speech(pcm_frame(1_000), 2_000)
    assert not frame_is_speech(b"", 2_000)
    assert not frame_is_speech(b"\x00", 2_000)
    assert not frame_is_speech(b"\x01\x02\x03", 2_000)


def test_utterance_buffer_endpoints_on_silence():
    buffer = UtteranceBuffer(FAST_CONFIG)
    assert buffer.add(silence()) is False
    assert not buffer.has_utterance
    assert buffer.add(loud()) is True
    buffer.add(loud())
    buffer.add(silence())
    buffer.add(silence())
    assert buffer.has_utterance
    assert not buffer.complete
    buffer.add(silence())
    assert buffer.complete
    assert len(buffer.pcm) == 5 * RATE // 10 * 2
    buffer.reset()
    assert not buffer.has_utterance
    assert not buffer.complete
    assert len(buffer.pcm) == 0


def test_utterance_buffer_caps_on_max_duration():
    config = RealtimeConfig(
        sample_rate=RATE,
        vad_threshold=2_000,
        silence_ms=10_000,
        min_speech_ms=100,
        max_utterance_ms=500,
    )
    buffer = UtteranceBuffer(config)
    buffer.add(loud())
    buffer.add(loud())
    buffer.add(silence())
    buffer.add(silence())
    buffer.add(silence())
    assert buffer.complete


def test_utterance_buffer_ignores_below_min_speech():
    config = RealtimeConfig(
        sample_rate=RATE,
        vad_threshold=2_000,
        silence_ms=10_000,
        min_speech_ms=300,
        max_utterance_ms=5_000,
    )
    buffer = UtteranceBuffer(config)
    buffer.add(loud())
    buffer.add(silence())
    buffer.add(silence())
    buffer.add(silence())
    assert buffer._saw_speech
    assert not buffer.has_utterance


def test_voice_turn_audit_payload_json_is_valid(scripted_llm, scripted_speech):
    speech = scripted_speech(
        [Transcript(text="aa jaana", confidence=0.9, latency_ms=70)],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=30)],
    )
    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            ws.receive_json()  # turn
            ws.receive_json()  # agent_audio
        events = client.get(f"/api/sessions/{session_id}/events").json()
        voice_events = [event for event in events if event["event_type"] == "VOICE_TURN"]
        assert len(voice_events) == 1
        payload = json.loads(voice_events[0]["payload_json"])
        assert payload["stt_ms"] == 70
        assert payload["audio_error"] is None
        assert payload["audio_bytes"] == 5


def test_streaming_stt_path_skips_rest_transcription(scripted_llm, scripted_speech):
    stream = ScriptedStream(
        [
            StreamingTranscript(
                text="6 se 8 aa jaana", language="hi-IN", latency_ms=240
            )
        ]
    )
    speech = scripted_speech(
        [],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=40)],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
        streaming=stream,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            turn = ws.receive_json()
            assert turn["type"] == "turn"
            assert turn["transcript"] == "6 se 8 aa jaana"
            assert turn["detected_language"] == "hi-IN"
            assert turn["transcript_confidence"] is None
            assert turn["timings"]["stt_ms"] == 240
            assert turn["planner"]["action"] == "PROPOSE_PLAN"
            assert ws.receive_json()["type"] == "agent_audio"

        events = client.get(f"/api/sessions/{session_id}/events").json()
        payload = json.loads(
            next(e for e in events if e["event_type"] == "VOICE_TURN")["payload_json"]
        )
        assert payload["stt_mode"] == "streaming"

    assert stream.connect_attempts == 1
    assert stream.commits == 1
    assert stream.frames == [loud(), loud(), loud(), silence(), silence(), silence()]
    assert stream.closed == 1
    assert speech.transcribe_calls == []
    assert speech.synthesize_calls == [turn["agent_message"]]


def test_streaming_failure_falls_back_to_rest(scripted_llm, scripted_speech):
    stream = ScriptedStream([], finish_error="connection lost")
    speech = scripted_speech(
        [Transcript(text="6 se 8 aa jaana", confidence=0.9, latency_ms=110)],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=40)],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
        streaming=stream,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            turn = ws.receive_json()
            assert turn["transcript"] == "6 se 8 aa jaana"
            assert turn["transcript_confidence"] == 0.9
            assert ws.receive_json()["type"] == "agent_audio"
        events = client.get(f"/api/sessions/{session_id}/events").json()
        types = [event["event_type"] for event in events]
        assert "STREAMING_STT_FAILED" in types

    assert stream.commits == 1
    assert speech.transcribe_calls  # REST was used after the streaming failure


def test_streaming_no_text_falls_back_to_rest(scripted_llm, scripted_speech):
    stream = ScriptedStream([None])
    speech = scripted_speech(
        [Transcript(text="theek hai", confidence=0.9, latency_ms=80)],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=40)],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
        streaming=stream,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            assert ws.receive_json()["transcript"] == "theek hai"
            ws.receive_json()  # agent_audio

    assert stream.commits == 1
    assert len(speech.transcribe_calls) == 1


def test_streaming_connect_failure_uses_rest_and_retries_on_start(
    scripted_llm, scripted_speech
):
    stream = ScriptedStream([], connect_error="refused")
    speech = scripted_speech(
        [Transcript(text="theek hai", confidence=0.9, latency_ms=80)],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=40)],
    )

    with realtime_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
        streaming=stream,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            send_utterance(ws)
            ws.receive_json()  # speech_start
            ws.receive_json()  # speech_end
            assert ws.receive_json()["transcript"] == "theek hai"
            ws.receive_json()  # agent_audio
        events = client.get(f"/api/sessions/{session_id}/events").json()
        types = [event["event_type"] for event in events]
        assert types.count("STREAMING_STT_UNAVAILABLE") == 2

    assert stream.commits == 0
    assert stream.frames == []
    assert len(speech.transcribe_calls) == 1


def test_reset_cancels_the_open_streaming_utterance(scripted_llm, scripted_speech):
    stream = ScriptedStream([])
    speech = scripted_speech([], [])

    with realtime_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
        streaming=stream,
    ) as client:
        session_id = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}/voice") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "start"})
            ws.receive_json()  # listening
            ws.send_bytes(loud())
            ws.receive_json()  # speech_start
            ws.send_json({"type": "reset"})

    assert stream.cancelled == 1
    assert stream.commits == 0
    assert speech.transcribe_calls == []
