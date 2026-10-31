from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.dialer import Dialer
from app.main import create_app
from app.models import DialerEvent, Order
from app.seed import seed_database

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


@pytest.fixture
def demo(tmp_path, scripted_llm):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'demo.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db)
    llm = scripted_llm(
        [
            {
                "action": "PROPOSE_PLAN",
                "message": "Understood. {plan_summary} {confirmation_question}",
                "confidence": 0.95,
                "escalation_reason": None,
                "proposal": {
                    "primary": {
                        "action": "HAND_TO_CUSTOMER",
                        "window_start": (NOW + timedelta(minutes=15)).isoformat(),
                        "window_end": (NOW + timedelta(minutes=135)).isoformat(),
                    }
                },
            }
        ]
    )
    dialer = Dialer(engine, clock=lambda: NOW, interval_seconds=10)
    app = create_app(init_database=False, llm_client=llm, speech_client=None, dialer=dialer)

    def sessions():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        yield client, dialer, engine
    engine.dispose()


@pytest.mark.parametrize("payment", ["COD", "PREPAID"])
def test_checkout_creates_only_synthetic_pending_order_with_relative_windows(demo, payment):
    client, _, engine = demo
    response = client.post("/api/orders", json={"payment_mode": payment, "requires_otp": True})
    assert response.status_code == 201
    order = response.json()
    assert order["merchant_name"] == "SwiftCart"
    assert order["status"] == "PENDING" and order["payment_mode"] == payment
    assert datetime.fromisoformat(order["window_start"]) == NOW + timedelta(minutes=15)
    assert datetime.fromisoformat(order["window_end"]) == NOW + timedelta(minutes=135)
    assert len(client.get(f"/api/orders/{order['id']}/slots").json()) == 2
    with Session(engine) as db:
        assert db.get(Order, 1042).window_start.date().isoformat() == "2026-11-05"


@pytest.mark.parametrize(
    "payload",
    [
        {"payment_mode": "INVALID"},
        {"payment_mode": "COD", "phone": "synthetic-extra"},
        {"payment_mode": "PREPAID", "address": "custom-extra"},
    ],
)
def test_checkout_rejects_personal_or_unrecognized_fields(demo, payload):
    assert demo[0].post("/api/orders", json=payload).status_code == 422


def test_readiness_is_deliberate_and_idempotent_and_does_not_touch_seed_order(demo):
    client, dialer, engine = demo
    order = client.post("/api/orders", json={"payment_mode": "COD"}).json()
    assert dialer.sweep() == []
    route = f"/api/orders/{order['id']}/out-for-delivery"
    assert client.post(route).json()["status"] == "OUT_FOR_DELIVERY"
    assert client.post(route).status_code == 200
    assert len(dialer.sweep()) == 1
    assert client.post("/api/orders/1042/out-for-delivery").status_code == 409
    with Session(engine) as db:
        events = db.exec(
            select(DialerEvent).where(
                DialerEvent.order_id == order["id"], DialerEvent.event_type == "ORDER_READY"
            )
        ).all()
        assert len(events) == 1


def test_dashboard_tracks_plan_confirmation_commit_and_safe_transcript(demo):
    client, dialer, _ = demo
    order = client.post("/api/orders", json={"payment_mode": "COD"}).json()
    client.post(f"/api/orders/{order['id']}/out-for-delivery")
    sid = dialer.sweep()[0]
    calls = client.get("/api/sessions", params={"order_id": order["id"], "active": True}).json()
    assert calls[0]["id"] == sid and calls[0]["transport"] == "browser"
    client.post(f"/api/sessions/{sid}/connect")
    client.post(f"/api/sessions/{sid}/context-confirmation")
    offered = client.post(
        f"/api/sessions/{sid}/agent-turn", json={"text": "Please hand it directly to me"}
    )
    assert offered.json()["turn"]["verdict"]["valid"]
    turns = client.get(f"/api/sessions/{sid}/turns").json()
    assert turns[-1]["structured_payload"]["planner"]["wording_source"] == "model"
    assert turns[-1]["structured_payload"]["planner"]["response_language"] == "en-IN"
    details = client.get("/api/sessions", params={"order_id": order["id"]}).json()[0]
    assert (
        details["verdict"]["valid"]
        and details["proposal"]["primary"]["action"] == "HAND_TO_CUSTOMER"
    )
    client.post(f"/api/sessions/{sid}/agent-turn", json={"text": "yes"})
    assert (
        client.post(
            f"/api/sessions/{sid}/commit", json={"idempotency_key": "dashboard-demo-commit"}
        ).status_code
        == 200
    )
    completed = client.get(
        "/api/sessions", params={"active": False, "order_id": order["id"]}
    ).json()
    assert (
        completed[0]["customer_confirmed"]
        and completed[0]["courier_card"]["order_id"] == order["id"]
    )
    assert (
        client.get("/api/sessions", params={"active": True, "order_id": order["id"]}).json() == []
    )
    assert client.get("/api/dialer/events", params={"order_id": order["id"]}).json()


def test_dashboard_bounds_and_unknown_session(demo):
    client = demo[0]
    assert client.get("/api/sessions?limit=101").status_code == 422
    assert client.get("/api/dialer/events?limit=101").status_code == 422
    assert client.get("/api/sessions/missing/turns").status_code == 404
    assert client.get("/api/sessions?order_id=9999").json() == []


def test_high_value_parcel_price_is_controlled_by_server(demo):
    response = demo[0].post(
        "/api/orders", json={"payment_mode": "PREPAID", "parcel_type": "high_value"}
    )
    assert response.status_code == 201
    assert response.json()["declared_value_inr"] == 30000


def test_dashboard_redacts_legacy_transcript_and_supports_cursor(demo):
    from app.calls import CallService
    from app.models import Speaker, Turn

    client, _, engine = demo
    with Session(engine) as db:
        sid = CallService(db).create_call(1042).id
        db.add(
            Turn(
                session_id=sid,
                speaker=Speaker.CUSTOMER,
                text="My OTP is 123456",
                structured_payload_json='{"text":"My OTP is 123456"}',
                created_at=NOW,
            )
        )
        db.commit()
    response = client.get(f"/api/sessions/{sid}/turns?limit=1")
    assert response.status_code == 200
    assert "123456" not in response.text
    assert "[redacted]" in response.text
    cursor = response.json()[0]["id"]
    assert client.get(f"/api/sessions/{sid}/turns?after_id={cursor}").json() == []
