from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from test_telephony import engine as phone_engine
from test_telephony import phone as phone_fixture

from app.calls import utc_now
from app.config import settings
from app.db import get_session
from app.main import create_app
from app.models import PhoneDemoApproval, PhoneDemoPermit, TwilioBinding
from app.telephony import TwilioTransport

engine = phone_engine
phone = phone_fixture


def permit(phone, *, expired=False):
    transport, cfg, sid, requests = phone
    cfg.twilio_approved_session_id = ""
    with Session(transport.engine) as db:
        db.add(PhoneDemoPermit(armed_at=utc_now(), expires_at=utc_now() + timedelta(minutes=-1 if expired else 15), remaining_calls=1))
        db.commit()
    return transport, cfg, sid, requests


def test_automatic_permit_dispatches_once_and_survives_transport_restart(phone):
    transport, cfg, sid, requests = permit(phone)
    assert transport.dispatch_if_approved(sid)
    assert len(requests) == 1
    with Session(transport.engine) as db:
        assert db.get(PhoneDemoPermit, 1).remaining_calls == 0
        assert db.get(PhoneDemoApproval, sid)
    restarted = TwilioTransport(transport.engine, cfg, transport=transport.client._transport)
    try:
        assert not restarted.dispatch_if_approved(sid)
        assert len(requests) == 1
    finally:
        restarted.close()


def test_expired_permit_never_dials(phone):
    transport, _, sid, requests = permit(phone, expired=True)
    assert not transport.dispatch_if_approved(sid)
    assert requests == []


def test_permit_never_overrides_streaming_gate(phone):
    transport, cfg, sid, requests = permit(phone)
    cfg.twilio_streaming_verified = False
    assert not transport.dispatch_if_approved(sid)
    with Session(transport.engine) as db:
        assert db.get(PhoneDemoPermit, 1).remaining_calls == 1
    assert requests == []


@pytest.fixture
def control(phone, monkeypatch):
    transport, cfg, _, _ = phone
    cfg.twilio_approved_session_id = ""
    monkeypatch.setattr(settings, "operator_api_token", "test-operator-only")
    app = create_app(init_database=False, telephony=transport, llm_client=None, speech_client=None)
    def session():
        with Session(transport.engine) as db:
            yield db
    app.dependency_overrides[get_session] = session
    with TestClient(app) as client:
        yield client, transport


def test_arm_requires_auth_and_respects_call_and_expiry_bounds(control):
    client, _ = control
    assert client.post('/api/operator/phone/arm', json={}).status_code == 401
    headers = {'X-Operator-Token': 'test-operator-only'}
    assert client.post('/api/operator/phone/arm', headers=headers, json={'max_calls': 4}).status_code == 422
    assert client.post('/api/operator/phone/arm', headers=headers, json={'expires_minutes': 31}).status_code == 422
    response = client.post('/api/operator/phone/arm', headers=headers, json={})
    assert response.status_code == 200
    assert response.json()['remaining_calls'] == 1
    assert client.post('/api/operator/phone/disarm', headers=headers).json()['armed'] is False


def test_disarm_revokes_reserved_but_not_dispatched_approval(control, phone):
    client, transport = control
    _, _, sid, _ = phone
    headers = {'X-Operator-Token': 'test-operator-only'}
    client.post('/api/operator/phone/arm', headers=headers, json={})
    transport._reserve_automatic_approval(sid)
    assert transport.is_approved(sid)
    assert client.post('/api/operator/phone/disarm', headers=headers).status_code == 200
    assert not transport.is_approved(sid)


def test_arm_blocked_until_phone_setup_is_ready(control):
    client, transport = control
    transport.settings.twilio_streaming_verified = False
    response = client.post('/api/operator/phone/arm', headers={'X-Operator-Token': 'test-operator-only'}, json={})
    assert response.status_code == 409


def test_disarm_also_blocks_static_session_approval_after_restart(control, phone):
    client, transport = control
    _, config, sid, requests = phone
    config.twilio_approved_session_id = sid
    assert transport.is_approved(sid)
    client.post('/api/operator/phone/disarm', headers={'X-Operator-Token': 'test-operator-only'})
    restarted = TwilioTransport(transport.engine, config, transport=transport.client._transport)
    try:
        assert not restarted.is_approved(sid)
        assert not restarted.dispatch_if_approved(sid)
        assert requests == []
    finally:
        restarted.close()


def test_disarm_between_approval_check_and_binding_reservation_prevents_call(phone, monkeypatch):
    transport, _, sid, requests = permit(phone)
    original = transport.is_approved
    checks = 0
    def raced_check(session_id):
        nonlocal checks
        checks += 1
        allowed = original(session_id)
        if checks == 1:
            with Session(transport.engine) as db:
                db.get(PhoneDemoPermit, 1).remaining_calls = 0
                approval = db.get(PhoneDemoApproval, sid)
                db.delete(approval)
                db.commit()
        return allowed
    monkeypatch.setattr(transport, 'is_approved', raced_check)
    assert not transport.dispatch_if_approved(sid)
    assert requests == []
    with Session(transport.engine) as db:
        assert db.get(TwilioBinding, sid) is None
