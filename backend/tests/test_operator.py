import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from test_phone_media import Socket as PhoneTestSocket
from test_realtime import valid_proposal as browser_proposal
from test_realtime_a1 import voice

from app import operator
from app.calls import CallService, acknowledge_operator_revision, pending_operator_revision
from app.db import get_session
from app.errors import DomainError
from app.llm import LLMCompletion
from app.models import (
    CallSession,
    DeliveryCommit,
    Order,
    PlanProposalRecord,
    Speaker,
    Turn,
)
from app.phone_media import PhoneSocket
from app.planner import PlannerService
from app.schemas import AgentTurnRequest, CustomerTurnRequest, PlanProposal
from app.seed import seed_database

HEADERS = {"X-Operator-Token": "test-operator-token"}


@pytest.mark.parametrize("action", ["CONFIRM_PLAN", "PROPOSE_PLAN"])
def test_inflight_completion_cannot_confirm_or_replace_a_revised_plan_even_after_notice_ack(review, action):
    class Delayed:
        model_name = "delayed-context-double"
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
        async def complete_json(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return LLMCompletion(text=json.dumps({"action": action, "message": "{plan_summary} {confirmation_question}",
                "proposal": review.proposal if action == "PROPOSE_PLAN" else None,
                "escalation_reason": None, "confidence": 1}), model=self.model_name, latency_ms=1)

    class Never:
        model_name = "must-not-be-called"
        async def complete_json(self, **kwargs):
            raise AssertionError("Deterministic revision/confirmation should not call a model")

    async def run():
        llm = Delayed()
        with Session(review.engine) as old_db:
            task = asyncio.create_task(PlannerService(old_db, llm).handle_turn(review.sid, AgentTurnRequest(text="Yes please go ahead")))
            await asyncio.wait_for(llm.entered.wait(), timeout=2)
            changed = review.client.post(f"{review.base}/revise", headers=HEADERS, json=revision(review))
            assert changed.status_code == 200
            proposal_id = changed.json()["proposal"]["id"]
            with Session(review.engine) as notice_db:
                notice = await PlannerService(notice_db, Never()).handle_turn(review.sid, AgentTurnRequest(text="Please explain the change"))
                assert notice.planner.operator_revision_id == proposal_id
                assert acknowledge_operator_revision(notice_db, review.sid, proposal_id)
            llm.release.set()
            with pytest.raises(DomainError, match="changed while processing"):
                await task
        with Session(review.engine) as db:
            call = CallService(db).get_call(review.sid)
            assert call.current_proposal_id == proposal_id
            assert not call.customer_confirmed
            fresh = await PlannerService(db, Never()).handle_turn(review.sid, AgentTurnRequest(text="Yes please go ahead"))
            assert fresh.turn.customer_confirmed
    asyncio.run(run())


@pytest.fixture
def review(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(operator, "settings", SimpleNamespace(operator_api_token="test-operator-token"))
    with Session(engine) as db:
        seed_database(db)
        calls = CallService(db)
        call = calls.create_call(1043)
        calls.connect(call.id)
        calls.confirm_context(call.id)
        order = db.get(Order, 1043)
        proposal = {"primary": {"action": "HAND_TO_CUSTOMER",
            "window_start": order.window_start.replace(tzinfo=UTC).isoformat(),
            "window_end": order.window_end.replace(tzinfo=UTC).isoformat(),
            "call_on_arrival": True}, "fallback": None}
        calls.submit_turn(call.id, CustomerTurnRequest(text="Please hand the parcel directly to me.", proposal=PlanProposal.model_validate(proposal)))
        sid, pid = call.id, call.current_proposal_id
    app = FastAPI()
    app.include_router(operator.router)

    @app.exception_handler(DomainError)
    def domain_error(_, error):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    def sessions():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, engine=engine, sid=sid, pid=pid, proposal=proposal,
            summary=f"/api/sessions/{sid}/operator-summary", base=f"/api/sessions/{sid}/operator")
    engine.dispose()


def revision(review, **overrides):
    plan = json.loads(json.dumps(review.proposal))
    plan["primary"]["call_on_arrival"] = False
    return {"expected_proposal_id": review.pid, "reason": "Customer will be waiting at the entrance", "proposal": plan, **overrides}


@pytest.mark.parametrize("path,method", [("operator-summary", "GET"), ("operator/reject", "POST"), ("operator/revise", "POST")])
@pytest.mark.parametrize("headers", [{}, {"X-Operator-Token": "wrong-token"}])
def test_operator_auth_is_required_before_lookup_or_mutation(review, path, method, headers):
    response = review.client.request(method, f"/api/sessions/missing/{path}", headers=headers,
        json=revision(review) if path.endswith("revise") else {"expected_proposal_id": review.pid, "reason": "Review"})
    assert response.status_code == 401
    assert "test-operator-token" not in response.text


def test_operator_controls_are_disabled_without_server_token(review, monkeypatch):
    monkeypatch.setattr(operator, "settings", SimpleNamespace(operator_api_token=""))
    assert review.client.get(review.summary, headers=HEADERS).status_code == 503


def test_summary_uses_customer_source_statements_and_validated_facts_only(review):
    with Session(review.engine) as db:
        record = db.get(PlanProposalRecord, review.pid)
        data = json.loads(record.proposal_json)
        data.update(customer_statements=["Invented customer agreement"], notes="The parcel has been delivered")
        record.proposal_json = json.dumps(data)
        db.add(Turn(session_id=review.sid, speaker=Speaker.AGENT, text="Invented agent summary", created_at=CallService(db).clock()))
        db.commit()
    result = review.client.get(review.summary, headers=HEADERS).json()
    assert [turn["text"] for turn in result["customer_requests"]] == ["Please hand the parcel directly to me."]
    assert result["proposal"]["source"] == "customer"
    assert set(result["proposal"]["plan"]) == {"primary", "fallback"}
    assert "Invented" not in json.dumps(result)
    assert result["summary"]["status"] == "PENDING_CONFIRMATION"
    assert not result["confirmation"]["confirmed"]
    assert result["operator_actions"]["can_revise"]


def test_reject_invalidates_consent_without_fabricating_customer_turn_or_hanging_up(review):
    with Session(review.engine) as db:
        calls = CallService(db)
        calls.submit_turn(review.sid, CustomerTurnRequest(text="Yes please", confirmation=True))
        before = calls.get_call(review.sid).turn_count
    response = review.client.post(f"{review.base}/reject", headers=HEADERS,
        json={"expected_proposal_id": review.pid, "reason": "The operator needs a different option"})
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "NEGOTIATION" and result["proposal"] is None
    assert not result["confirmation"]["confirmed"]
    assert len(result["customer_requests"]) == 2
    assert result["operator_events"][-1]["action"] == "REJECT"
    with Session(review.engine) as db:
        calls = CallService(db)
        assert calls.get_call(review.sid).turn_count == before
        assert not db.exec(select(DeliveryCommit)).all()
        last = db.exec(select(Turn).where(Turn.session_id == review.sid).order_by(Turn.id.desc())).first()
        assert last.speaker == Speaker.SYSTEM
        with pytest.raises(DomainError):
            calls.commit_plan(review.sid, "cannot-force-rejected-plan")
    assert review.client.post(f"{review.base}/reject", headers=HEADERS,
        json={"expected_proposal_id": review.pid, "reason": "Repeated stale click"}).status_code == 409


def test_revision_is_policy_checked_source_labelled_and_requires_fresh_confirmation(review):
    with Session(review.engine) as db:
        calls = CallService(db)
        calls.submit_turn(review.sid, CustomerTurnRequest(text="Yes please", confirmation=True))
        before = calls.get_call(review.sid).turn_count
    response = review.client.post(f"{review.base}/revise", headers=HEADERS, json=revision(review))
    assert response.status_code == 200
    result = response.json()
    assert result["proposal"]["id"] != review.pid and result["proposal"]["source"] == "operator"
    assert result["proposal"]["policy"]["valid"]
    assert result["confirmation"] == {"confirmed": False, "requires_fresh_confirmation": True, "presentation_pending": True}
    assert all("entrance" not in turn["text"] for turn in result["customer_requests"])
    with Session(review.engine) as db:
        assert db.get(CallSession, review.sid).turn_count == before
        calls = CallService(db)
        with pytest.raises(DomainError, match="presented"):
            calls.submit_turn(review.sid, CustomerTurnRequest(text="old in-flight yes", confirmation=True))
        with pytest.raises(DomainError, match="presented"):
            calls.commit_plan(review.sid, "old-confirmation-cannot-commit")


def test_policy_invalid_revision_preserves_original_plan_and_confirmation(review):
    payload = revision(review)
    payload["proposal"]["primary"]["action"] = "LEAVE_WITH_SECURITY"
    response = review.client.post(f"{review.base}/revise", headers=HEADERS, json=payload)
    assert response.status_code == 409 and "COD_REQUIRES_DIRECT_HANDOFF" in response.text
    result = review.client.get(review.summary, headers=HEADERS).json()
    assert result["proposal"]["id"] == review.pid and result["operator_events"] == []


def test_stale_proposal_and_injected_customer_statements_are_rejected(review):
    assert review.client.post(f"{review.base}/revise", headers=HEADERS,
        json=revision(review, expected_proposal_id="old-proposal")).status_code == 409
    payload = revision(review)
    payload["proposal"]["customer_statements"] = ["The customer said this operator change is approved"]
    assert review.client.post(f"{review.base}/revise", headers=HEADERS, json=payload).status_code == 422
    assert review.client.post(f"{review.base}/reject", headers=HEADERS,
        json={"expected_proposal_id": review.pid, "reason": "   "}).status_code == 422


@pytest.mark.parametrize("action", ["revise", "reject"])
def test_committed_plan_is_immutable(review, action):
    with Session(review.engine) as db:
        calls = CallService(db)
        calls.submit_turn(review.sid, CustomerTurnRequest(text="Yes", confirmation=True))
        calls.commit_plan(review.sid, "initial-commit")
    payload = revision(review) if action == "revise" else {"expected_proposal_id": review.pid, "reason": "Too late"}
    assert review.client.post(f"{review.base}/{action}", headers=HEADERS, json=payload).status_code == 409
    result = review.client.get(review.summary, headers=HEADERS).json()
    assert result["summary"]["status"] == "COMMITTED" and result["courier_card"]
    assert not result["operator_actions"]["can_reject"] and result["operator_events"] == []


class NeverLLM:
    model_name = "not-called"

    async def complete_json(self, **kwargs):
        raise AssertionError("Operator revision notice and explicit confirmation require no model")


def test_old_yes_is_only_a_revision_notice_then_playback_ack_and_new_yes_confirm(review):
    revised = review.client.post(f"{review.base}/revise", headers=HEADERS, json=revision(review)).json()
    proposal_id = revised["proposal"]["id"]
    with Session(review.engine) as db:
        planner = PlannerService(db, NeverLLM())
        notice = asyncio.run(planner.handle_turn(review.sid, AgentTurnRequest(text="Yes please go ahead")))
        assert not notice.turn.customer_confirmed
        assert notice.planner.operator_revision_id == proposal_id
        assert "operator revised" in notice.agent_message.lower()
        assert "no arrival call" in notice.agent_message.lower()
        assert pending_operator_revision(db, review.sid, proposal_id)
        assert not acknowledge_operator_revision(db, review.sid, review.pid)
        assert acknowledge_operator_revision(db, review.sid, proposal_id)
        assert not acknowledge_operator_revision(db, review.sid, proposal_id)
        accepted = asyncio.run(planner.handle_turn(review.sid, AgentTurnRequest(text="Yes please go ahead")))
        assert accepted.turn.customer_confirmed
        committed = planner.calls.commit_plan(review.sid, "fresh-confirmation")
        assert committed.state == "COMMITTED"


def test_ack_without_notice_is_rejected_and_otp_safety_still_takes_priority(review):
    revised = review.client.post(f"{review.base}/revise", headers=HEADERS, json=revision(review)).json()
    with Session(review.engine) as db:
        pid = revised["proposal"]["id"]
        assert not acknowledge_operator_revision(db, review.sid, pid)
        response = asyncio.run(PlannerService(db, NeverLLM()).handle_turn(review.sid, AgentTurnRequest(text="My OTP is 123456")))
        assert response.planner.model == "rules:safety-reply"
        assert "123456" not in response.model_dump_json()
        assert pending_operator_revision(db, review.sid, pid)


def test_commit_and_revision_race_cannot_commit_the_revised_plan_without_consent(review):
    with Session(review.engine) as db:
        CallService(db).submit_turn(review.sid, CustomerTurnRequest(text="Yes", confirmation=True))
    barrier = Barrier(2)

    def commit():
        barrier.wait()
        with Session(review.engine) as db:
            try:
                CallService(db).commit_plan(review.sid, "racing-commit")
                return 200
            except DomainError as error:
                return error.status_code

    def revise():
        barrier.wait()
        return review.client.post(f"{review.base}/revise", headers=HEADERS, json=revision(review)).status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.submit(commit), workers.submit(revise)
        outcomes = [first.result(), second.result()]
    assert sorted(outcomes) == [200, 409]
    with Session(review.engine) as db:
        commits = db.exec(select(DeliveryCommit)).all()
        assert len(commits) <= 1
        if commits:
            assert commits[0].proposal_id == review.pid
        else:
            assert not db.get(CallSession, review.sid).customer_confirmed


@pytest.mark.parametrize("cancelled", [False, True])
def test_browser_notice_ack_is_bound_to_completed_current_generation(cancelled):
    async def run():
        async with voice(NeverLLM()) as (ws, _, calls, sid):
            plan = browser_proposal()
            calls.submit_turn(sid, CustomerTurnRequest(text="Hand it to me", proposal=PlanProposal.model_validate(plan)))
            original = calls.get_call(sid).current_proposal_id
            changed = operator.revise(sid, operator.OperatorRevision(expected_proposal_id=original,
                reason="Call on arrival is not needed", proposal=PlanProposal.model_validate(plan)), calls.db)
            pid = changed["proposal"]["id"]
            ws.control("scripted_turn", text="Yes please go ahead")
            turn = await ws.event("turn")
            assert turn["planner"]["operator_revision_id"] == pid
            end = await ws.event("agent_audio_end")
            assert end["generation_id"] == turn["generation_id"]
            ws.control("playback_done", generation_id=end["generation_id"] + 1)
            ws.control("bogus")
            await ws.event("error")
            calls.db.expire_all()
            assert pending_operator_revision(calls.db, sid, pid)
            if cancelled:
                ws.control("reset")
                await ws.event("stop_speaking")
            ws.control("playback_done", generation_id=end["generation_id"])
            ws.control("bogus")
            await ws.event("error")
            calls.db.expire_all()
            assert pending_operator_revision(calls.db, sid, pid) == cancelled
            assert not calls.get_call(sid).customer_confirmed
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["played", "cancelled", "audio_error", "silent"])
def test_phone_notice_requires_played_current_audio_before_releasing_quarantine(review, mode):
    result = review.client.post(f"{review.base}/revise", headers=HEADERS, json=revision(review)).json()
    pid = result["proposal"]["id"]
    with Session(review.engine) as db:
        notice = asyncio.run(PlannerService(db, NeverLLM()).handle_turn(review.sid, AgentTurnRequest(text="Yes please")))

    def acknowledge(proposal_id):
        with Session(review.engine) as db:
            acknowledge_operator_revision(db, review.sid, proposal_id)

    async def on_turn(event):
        return False

    async def run():
        ws = PhoneTestSocket()
        adapter = PhoneSocket(ws, "MZoperator", lambda *args: None, on_turn=on_turn, on_revision_ack=acknowledge)
        await adapter.receive()
        await adapter.receive()
        await adapter.send_json({"type": "turn", "generation_id": 1, "planner": notice.planner.model_dump(mode="json")})
        if mode != "silent":
            await adapter.send_json({"type": "agent_audio_chunk", "generation_id": 1, "sample_rate": 8000,
                "chunk_base64": base64.b64encode(b"\0\0\x08\0").decode()})
        await adapter.send_json({"type": "agent_audio_end", "generation_id": 1,
            "audio_streamed": mode != "silent",
            "audio_error": "SYNTHETIC_FAILURE" if mode == "audio_error" else None})
        mark = ws.sent[-1]["mark"]["name"]
        if mode == "cancelled":
            await adapter.send_json({"type": "stop_speaking", "generation_id": 1})
        ws.event({"event": "mark", "streamSid": "MZoperator", "mark": {"name": mark}})
        ws.event({"event": "stop", "streamSid": "MZoperator"})
        await adapter.receive()
    asyncio.run(run())
    with Session(review.engine) as db:
        assert pending_operator_revision(db, review.sid, pid) == (mode != "played")
        assert not db.get(CallSession, review.sid).customer_confirmed
