import base64
import json
from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.gnani import (
    GnaniGatewayError,
    GnaniTimeoutError,
    SpeechAudio,
    Transcript,
)
from app.main import create_app
from app.seed import seed_database

START = datetime.fromisoformat("2026-11-05T18:00:00+05:30")
END = datetime.fromisoformat("2026-11-05T20:00:00+05:30")
AUDIO = b"RIFF\x00\x00\x00\x00WAVEfmt "


def decision(action: str, message: str, proposal: dict | None = None) -> dict:
    return {
        "action": action,
        "message": message,
        "proposal": proposal,
        "escalation_reason": None,
        "confidence": 0.92,
    }


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
    return decision(
        "PROPOSE_PLAN",
        "Delivery ho gayi!",
        proposal=valid_proposal(),
    )


@contextmanager
def voice_client(
    scripted_llm,
    *,
    llm_outcomes: list[object],
    speech,
    max_audio_bytes: int = 10_000_000,
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
        max_audio_bytes=max_audio_bytes,
    )
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client


def start_session(client: TestClient) -> str:
    created = client.post("/api/orders/1042/call")
    assert created.status_code == 201
    session_id = created.json()["id"]
    client.post(f"/api/sessions/{session_id}/connect").raise_for_status()
    client.post(f"/api/sessions/{session_id}/context-confirmation").raise_for_status()
    return session_id


def audio_file():
    return {"audio": ("turn.wav", AUDIO, "audio/wav")}


def test_voice_turn_runs_stt_planner_policy_and_tts(scripted_llm, scripted_speech):
    speech = scripted_speech(
        [
            Transcript(
                text="6 se 8 baje direct de dena",
                language="hi-IN",
                confidence=0.93,
                latency_ms=120,
            )
        ],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=80)],
    )

    with voice_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["transcript"] == "6 se 8 baje direct de dena"
        assert body["detected_language"] == "hi-IN"
        assert body["transcript_confidence"] == 0.93
        assert body["state"] == "PENDING_CONFIRMATION"
        assert body["turn"]["verdict"]["valid"] is True
        assert "direct handoff" in body["agent_message"]
        assert "Delivery ho gayi" not in body["agent_message"]
        assert body["planner"]["action"] == "PROPOSE_PLAN"
        assert base64.b64decode(body["audio_base64"]) == b"AUDIO"
        assert body["audio_content_type"] == "audio/wav"
        assert body["audio_error"] is None
        assert body["timings"]["stt_ms"] == 120
        assert body["timings"]["total_ms"] >= 0

        assert speech.transcribe_calls == [
            {
                "bytes": len(AUDIO),
                "filename": "audio.wav",
                "content_type": "audio/wav",
            }
        ]
        assert speech.synthesize_calls == [body["agent_message"]]

        events = client.get(f"/api/sessions/{session_id}/events").json()
        voice_events = [event for event in events if event["event_type"] == "VOICE_TURN"]
        assert len(voice_events) == 1
        payload = json.loads(voice_events[0]["payload_json"])
        assert payload["stt_ms"] == 120
        assert payload["audio_error"] is None


def test_low_transcript_confidence_blocks_the_proposal(scripted_llm, scripted_speech):
    speech = scripted_speech(
        [Transcript(text="haan", confidence=0.2, latency_ms=90)],
        [SpeechAudio(data=b"AUDIO", content_type="audio/wav", latency_ms=10)],
    )

    with voice_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "DISCOVERY"
        assert body["planner"]["degraded"] is True
        assert body["planner"]["error_code"] == "LOW_TRANSCRIPT_CONFIDENCE"
        assert body["turn"]["turn_count"] == 1
        assert body["turn"]["verdict"] is None


def test_tts_failure_keeps_the_turn_and_returns_text(scripted_llm, scripted_speech):
    speech = scripted_speech(
        [Transcript(text="direct de dena", confidence=0.9, latency_ms=100)],
        [GnaniTimeoutError("slow tts")],
    )

    with voice_client(
        scripted_llm,
        llm_outcomes=[proposal_decision()],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["audio_base64"] is None
        assert body["audio_content_type"] is None
        assert body["audio_error"] == "TTS_TIMEOUT"
        assert body["state"] == "PENDING_CONFIRMATION"
        assert body["turn"]["verdict"]["valid"] is True
        assert body["agent_message"]

        events = client.get(f"/api/sessions/{session_id}/events").json()
        voice_events = [event for event in events if event["event_type"] == "VOICE_TURN"]
        payload = json.loads(voice_events[0]["payload_json"])
        assert payload["audio_error"] == "TTS_TIMEOUT"


def test_stt_failure_does_not_submit_a_turn(scripted_llm, scripted_speech):
    speech = scripted_speech([GnaniGatewayError("down")], [])

    with voice_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "Speech recognition is unavailable."
        session = client.get(f"/api/sessions/{session_id}").json()
        assert session["turn_count"] == 0
        assert speech.synthesize_calls == []


def test_empty_transcript_is_rejected_before_planning(scripted_llm, scripted_speech):
    speech = scripted_speech([Transcript(text="   ")], [])

    with voice_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "No speech was detected in the audio."
        assert speech.synthesize_calls == []


def test_unsupported_audio_type_is_rejected(scripted_llm, scripted_speech):
    speech = scripted_speech([], [])

    with voice_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files={"audio": ("turn.webm", AUDIO, "audio/webm")},
        )

        assert response.status_code == 415
        assert speech.transcribe_calls == []


def test_oversized_audio_is_rejected(scripted_llm, scripted_speech):
    speech = scripted_speech([], [])

    with voice_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
        max_audio_bytes=8,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 413
        assert speech.transcribe_calls == []


def test_voice_turn_requires_speech_configuration(scripted_llm):
    with voice_client(
        scripted_llm,
        llm_outcomes=[],
        speech=None,
    ) as client:
        session_id = start_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "Gnani speech is not configured"


def test_voice_turn_rejects_wrong_call_state(scripted_llm, scripted_speech):
    speech = scripted_speech([], [])

    with voice_client(
        scripted_llm,
        llm_outcomes=[],
        speech=speech,
    ) as client:
        created = client.post("/api/orders/1042/call")
        session_id = created.json()["id"]
        response = client.post(
            f"/api/sessions/{session_id}/voice-turn",
            files=audio_file(),
        )

        assert response.status_code == 409
        assert speech.transcribe_calls == []
