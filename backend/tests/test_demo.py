from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.dialer import Dialer
from app.main import create_app
from app.models import DialerEvent, Order
from app.operator import require_operator_key
from app.seed import seed_database

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
DELIVERY_START = datetime.fromisoformat("2026-11-01T10:00:00+05:30")


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
                        "window_start": DELIVERY_START.isoformat(),
                        "window_end": (DELIVERY_START + timedelta(hours=2)).isoformat(),
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

    app.dependency_overrides[require_operator_key] = lambda: None
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
    assert order["requires_otp"] == (payment == "PREPAID")
    assert datetime.fromisoformat(order["window_start"]) == DELIVERY_START
    assert datetime.fromisoformat(order["window_end"]) == DELIVERY_START + timedelta(hours=2)
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
    assert dialer.sweep() == []  # Scheduled later; do not auto-call early.
    dialer.clock = lambda: DELIVERY_START - timedelta(minutes=30)
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
    dialer.clock = lambda: DELIVERY_START - timedelta(minutes=30)
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


def test_catalog_contains_real_product_choices_and_server_owned_values(demo):
    client = demo[0]
    products = client.get('/api/catalog').json()
    assert {product['id'] for product in products} == {'headphones', 'backpack', 'watch', 'laptop'}
    for product in products:
        response = client.post('/api/orders', json={'payment_mode': 'COD', 'product_id': product['id'], 'requires_otp': True})
        assert response.status_code == 201
        order = response.json()
        assert order['declared_value_inr'] == product['price_inr']
        assert order['requires_otp'] is False
        saved = client.get(f"/api/orders/{order['id']}/product").json()
        assert saved['product_id'] == product['id']
        assert saved['product_name'] == product['name']
    assert next(product for product in products if product['id'] == 'laptop')['direct_handoff_required']


@pytest.mark.parametrize('payment,client_otp,expected', [('COD', True, False), ('PREPAID', False, True)])
def test_payment_rule_cannot_be_overridden_by_checkout_client(demo, payment, client_otp, expected):
    response = demo[0].post('/api/orders', json={'product_id': 'headphones', 'payment_mode': payment, 'requires_otp': client_otp})
    assert response.status_code == 201
    assert response.json()['requires_otp'] is expected


def test_unknown_product_and_client_price_are_rejected(demo):
    client = demo[0]
    assert client.post('/api/orders', json={'payment_mode': 'COD', 'product_id': 'unknown'}).status_code == 422
    assert client.post('/api/orders', json={'payment_mode': 'COD', 'product_id': 'laptop', 'price_inr': 1}).status_code == 422


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


def test_predispatch_call_requires_operator_access_without_dispatching(demo, monkeypatch):
    from types import SimpleNamespace

    from app import operator
    client, _, _ = demo
    client.app.dependency_overrides.pop(require_operator_key)
    monkeypatch.setattr(operator, "settings", SimpleNamespace(operator_api_token="demo-test-key"))
    order = client.post("/api/orders", json={"product_id": "backpack", "payment_mode": "PREPAID"}).json()
    ready = f"/api/orders/{order['id']}/out-for-delivery"
    prepare = f"/api/orders/{order['id']}/call"
    assert client.post(ready).status_code == 401
    assert client.post(prepare).status_code == 401
    headers = {"X-Operator-Token": "demo-test-key"}
    assert client.post(prepare, headers=headers).status_code == 201
    assert client.get(f"/api/orders/{order['id']}").json()["status"] == "PENDING"
    assert client.post(ready, headers=headers).status_code == 200
    assert client.post(prepare, headers=headers).status_code == 409
    assert client.get(f"/api/orders/{order['id']}/courier-instructions").status_code == 401
    assert client.get(f"/api/orders/{order['id']}/courier-instructions", headers=headers).json() is None
    listed = client.get('/api/orders?catalog_only=true').json()
    assert [item['id'] for item in listed] == [order['id']]


def test_recent_order_limit_preserves_the_legacy_unbounded_listing(demo):
    client, _, _ = demo
    first = client.post('/api/orders',json={'product_id':'backpack','payment_mode':'PREPAID'}).json()
    second = client.post('/api/orders',json={'product_id':'watch','payment_mode':'PREPAID'}).json()
    limited = client.get('/api/orders?catalog_only=true&limit=1').json()
    assert [item['id'] for item in limited]==[second['id']]
    all_orders = client.get('/api/orders').json()
    assert first['id'] in [item['id'] for item in all_orders]
    assert all_orders[0]['id'] < all_orders[-1]['id']
    assert client.get('/api/orders?limit=0').status_code==422

@pytest.mark.parametrize('already_dispatched', [False, True])
def test_rescheduling_updates_the_same_parcel_only_after_confirmation(demo, already_dispatched):
    client, _, _ = demo
    order = client.post('/api/orders', json={'product_id':'backpack','payment_mode':'PREPAID'}).json()
    order_id = order['id']
    before_count = len(client.get('/api/orders').json())
    if already_dispatched:
        assert client.post(f'/api/orders/{order_id}/out-for-delivery').status_code == 200
    call = client.post(f'/api/orders/{order_id}/call').json()
    sid = call['id']
    assert client.post(f'/api/sessions/{sid}/connect').status_code == 200
    assert client.post(f'/api/sessions/{sid}/context-confirmation').status_code == 200
    tomorrow = DELIVERY_START + timedelta(days=1, hours=7)
    proposed = client.post(f'/api/sessions/{sid}/turns', json={
        'text':'I cannot receive this appointment. Please deliver the next day at five.',
        'proposal':{'primary':{'action':'RESCHEDULE', 'window_start':tomorrow.isoformat(),
            'window_end':(tomorrow + timedelta(hours=1)).isoformat(), 'call_on_arrival':True}},
    })
    assert proposed.status_code == 200 and proposed.json()['verdict']['valid']
    assert client.get(f'/api/orders/{order_id}').json()['window_start'] == order['window_start']
    commit_path = f'/api/sessions/{sid}/commit'
    commit_body = {'idempotency_key':f'predispatch-reschedule-{already_dispatched}'}
    assert client.post(commit_path,json=commit_body).status_code == 409
    assert client.post(f'/api/sessions/{sid}/turns',json={'text':'Yes, confirm that date.','confirmation':True}).json()['customer_confirmed']
    committed = client.post(commit_path,json=commit_body)
    assert committed.status_code == 200
    saved = client.get(f'/api/orders/{order_id}').json()
    assert saved['id'] == order_id
    assert saved['status'] == ('RESCHEDULED' if already_dispatched else 'PENDING')
    assert datetime.fromisoformat(saved['window_start']) == tomorrow
    assert saved['prior_attempts'] == order['prior_attempts']
    assert saved['delivery_address_summary'] == order['delivery_address_summary']
    assert len(client.get('/api/orders').json()) == before_count
    instructions = client.get(f'/api/orders/{order_id}/courier-instructions').json()
    assert datetime.fromisoformat(instructions['courier_card']['window_start']) == datetime.fromisoformat(saved['window_start'])
    assert any('OTP' in line for line in instructions['courier_card']['instructions'])
    repeated = client.post(commit_path,json=commit_body).json()
    assert repeated['reused'] and repeated['commit_id'] == committed.json()['commit_id']
    # Dispatch is an independent operator action, including a revised next attempt.
    assert client.post(f'/api/orders/{order_id}/out-for-delivery').json()['status'] == 'OUT_FOR_DELIVERY'
