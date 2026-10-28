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


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lists_seeded_orders(client):
    response = client.get("/api/orders")
    assert response.status_code == 200
    assert len(response.json()) == 8
    assert response.json()[0]["id"] == 1042


def test_returns_available_slots(client):
    response = client.get("/api/orders/1042/slots")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_unknown_order_returns_404(client):
    assert client.get("/api/orders/9999").status_code == 404
    assert client.get("/api/orders/9999/slots").status_code == 404


def test_validation_endpoint_enforces_otp_policy(client):
    payload = {
        "proposal": {
            "primary": {
                "action": "HAND_TO_CUSTOMER",
                "window_start": "2026-11-05T18:00:00+05:30",
                "window_end": "2026-11-05T20:00:00+05:30",
            },
            "fallback": {
                "action": "LEAVE_WITH_SECURITY",
                "window_start": "2026-11-05T18:00:00+05:30",
                "window_end": "2026-11-05T20:00:00+05:30",
            },
        }
    }
    response = client.post("/api/orders/1042/validate-plan", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "OTP_REQUIRED_NO_THIRD_PARTY" in {
        violation["code"] for violation in body["violations"]
    }


def test_validation_endpoint_rejects_malformed_plan(client):
    response = client.post(
        "/api/orders/1042/validate-plan",
        json={"proposal": {"primary": {"action": "HAND_TO_CUSTOMER"}}},
    )
    assert response.status_code == 422
