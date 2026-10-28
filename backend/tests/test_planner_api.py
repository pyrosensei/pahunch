from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import create_app
from app.seed import seed_database

START = datetime.fromisoformat("2026-11-05T18:00:00+05:30")
END = datetime.fromisoformat("2026-11-05T20:00:00+05:30")


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


@contextmanager
def planner_client(scripted_llm, outcomes: list[object]):
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

    app = create_app(init_database=False, llm_client=scripted_llm(outcomes))
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client


def test_agent_turn_flow_over_http(scripted_llm):
    with planner_client(
        scripted_llm,
        [
            decision(
                "PROPOSE_PLAN",
                "Delivery ho gayi!",
                proposal=valid_proposal(),
            ),
            decision("CONFIRM_PLAN", "Haan theek hai."),
        ],
    ) as client:
        created = client.post("/api/orders/1042/call")
        session_id = created.json()["id"]
        client.post(f"/api/sessions/{session_id}/connect").raise_for_status()
        client.post(f"/api/sessions/{session_id}/context-confirmation").raise_for_status()

        proposed = client.post(
            f"/api/sessions/{session_id}/agent-turn",
            json={"text": "6 se 8 baje direct de dena, ek retry kar dena"},
        )
        assert proposed.status_code == 200
        proposed_body = proposed.json()
        assert proposed_body["state"] == "PENDING_CONFIRMATION"
        assert proposed_body["turn"]["verdict"]["valid"] is True
        assert "direct handoff" in proposed_body["agent_message"]
        assert "Delivery ho gayi" not in proposed_body["agent_message"]
        assert proposed_body["planner"]["action"] == "PROPOSE_PLAN"
        assert proposed_body["planner"]["degraded"] is False

        confirmed = client.post(
            f"/api/sessions/{session_id}/agent-turn",
            json={"text": "haan theek hai"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["turn"]["customer_confirmed"] is True

        committed = client.post(
            f"/api/sessions/{session_id}/commit",
            json={"idempotency_key": "api-planner-commit-01"},
        )
        assert committed.status_code == 200
        assert committed.json()["state"] == "COMMITTED"

        card = client.get(f"/api/sessions/{session_id}/courier-card")
        assert card.status_code == 200
        assert any("OTP" in line for line in card.json()["instructions"])

        events = client.get(f"/api/sessions/{session_id}/events")
        event_types = [event["event_type"] for event in events.json()]
        assert event_types.count("LLM_DECISION") == 1
        assert event_types.count("CONFIRMATION_RULE") == 1
        assert "ASSISTANT_MESSAGE" in event_types
        assert "PLAN_COMMITTED" in event_types
