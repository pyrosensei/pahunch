import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import create_app
from app.seed import seed_database


@pytest.fixture
def client():
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

    app = create_app(init_database=False)
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


def test_complete_call_flow_over_http(client):
    created = client.post("/api/orders/1042/call")
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert created.json()["trigger_reason"] == "PRIOR_FAILED_ATTEMPT"

    assert client.post(f"/api/sessions/{session_id}/connect").status_code == 200
    discovered = client.post(f"/api/sessions/{session_id}/context-confirmation")
    assert discovered.json()["state"] == "DISCOVERY"

    turn = client.post(
        f"/api/sessions/{session_id}/turns",
        json={
            "text": "6 se 8, direct handoff, ek retry",
            "proposal": {
                "primary": {
                    "action": "HAND_TO_CUSTOMER",
                    "window_start": "2026-11-05T18:00:00+05:30",
                    "window_end": "2026-11-05T20:00:00+05:30",
                },
                "fallback": {
                    "action": "RETRY_AFTER_DELAY",
                    "delay_minutes": 15,
                    "max_retries": 1,
                },
            },
        },
    )
    assert turn.status_code == 200
    assert turn.json()["state"] == "PENDING_CONFIRMATION"

    confirmed = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"text": "confirm", "confirmation": True},
    )
    assert confirmed.json()["customer_confirmed"] is True

    committed = client.post(
        f"/api/sessions/{session_id}/commit",
        json={"idempotency_key": "http-commit-001"},
    )
    assert committed.status_code == 200
    assert committed.json()["state"] == "COMMITTED"

    card = client.get(f"/api/sessions/{session_id}/courier-card")
    assert card.status_code == 200
    assert card.json()["order_id"] == 1042

    events = client.get(f"/api/sessions/{session_id}/events")
    assert events.status_code == 200
    assert len(events.json()) >= 5


def test_ineligible_order_returns_domain_error(client):
    response = client.post("/api/orders/1049/call")
    assert response.status_code == 409
    assert "Delivered or cancelled" in response.json()["detail"]
