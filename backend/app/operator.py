"""Authenticated operator review of pending plans; customer consent stays separate."""

import json
import secrets
from datetime import UTC
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlmodel import Session, select

from app.calls import ADMISSION_LOCK, CallService, pending_operator_revision
from app.config import settings
from app.db import get_session
from app.errors import DomainError
from app.language import redact_customer_text, redact_payload
from app.models import (
    AuditEvent,
    CallRuntime,
    CallSession,
    CallState,
    DeliveryCommit,
    DeliverySlot,
    Order,
    PlanProposalRecord,
    PolicyDecisionRecord,
    Speaker,
    Turn,
    TwilioBinding,
)
from app.policy import validate_plan
from app.schemas import PlanProposal, PlanStep
from app.state_machine import TERMINAL_STATES


def require_operator_key(
    token: Annotated[str | None, Header(alias="X-Operator-Token")] = None,
) -> None:
    expected = settings.operator_api_token
    if not expected:
        raise HTTPException(503, "Operator access has not been configured")
    if token is None or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "A valid operator token is required")


router = APIRouter(prefix="/api", dependencies=[Depends(require_operator_key)])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/operator/access")
def access():
    return {"authorized": True}


@router.get("/orders/{order_id}/courier-instructions")
def courier_instructions(order_id: int, db: SessionDep):
    order = db.get(Order, order_id)
    if order is None:
        raise DomainError(404, "Order not found")
    commit = db.exec(
        select(DeliveryCommit).join(CallSession, DeliveryCommit.session_id == CallSession.id)
        .where(CallSession.order_id == order_id)
        .order_by(DeliveryCommit.committed_at.desc(), DeliveryCommit.id.desc())
    ).first()
    if commit is None:
        return None
    return {
        "order_id": order_id, "session_id": commit.session_id,
        "commit_id": commit.id, "committed_at": commit.committed_at.replace(tzinfo=UTC) if commit.committed_at.tzinfo is None else commit.committed_at,
        "original_delivery_address": order.delivery_address_summary,
        "contact": None,
        "courier_card": redact_payload(json.loads(commit.courier_card_json)),
    }


class OperatorChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_proposal_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", "expected_proposal_id")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A nonblank value is required")
        return value


class OperatorRevision(OperatorChange):
    proposal: PlanProposal

    @model_validator(mode="after")
    def no_customer_attribution(self):
        if self.proposal.customer_statements:
            raise ValueError("Operator revisions cannot supply customer statements")
        return self


def _facts(proposal: PlanProposal) -> dict:
    # Notes, model confidences and asserted customer statements are not plan facts.
    data = proposal.model_dump(mode="json", include={"primary", "fallback"})
    for key in ("primary", "fallback"):
        if isinstance(data[key], dict):
            data[key].pop("notes", None)
    return redact_payload(data)


_VERIFIED_SOURCE_FIELDS = (
    ("named_recipient", "recipient_source"),
    ("recipient_address", "recipient_address_source"),
)


def _retain_verified_source(step: PlanStep | None, original: PlanStep | None) -> PlanStep | None:
    if step is None:
        return None
    data = step.model_dump()
    same_action = original is not None and original.action == step.action
    for field, source_field in _VERIFIED_SOURCE_FIELDS:
        source = getattr(original, source_field, None) if same_action else None
        preserved = (
            source is not None
            and getattr(original, field) == getattr(step, field)
            and (field != "recipient_address" or original.named_recipient == step.named_recipient)
        )
        data[source_field] = source.model_dump() if preserved else None
    return PlanStep.model_validate(data)


def _operator_facts(proposal: PlanProposal, original: PlanProposal | None) -> PlanProposal:
    reconciled = PlanProposal(
        primary=_retain_verified_source(proposal.primary, original.primary if original else None),
        fallback=_retain_verified_source(proposal.fallback, original.fallback if original else None),
        customer_statements=[],
        notes=proposal.notes,
        confidence={},
    )
    return PlanProposal.model_validate(_facts(reconciled))


def operator_summary(db: Session, session_id: str) -> dict:
    """Only stored state, source-labelled statements and validated plan fields."""
    calls = CallService(db)
    call = calls.get_call(session_id)
    runtime = db.get(CallRuntime, call.id)
    binding = db.get(TwilioBinding, call.id)
    turns = db.exec(select(Turn).where(Turn.session_id == call.id).order_by(Turn.id)).all()
    by_id = {turn.id: turn for turn in turns}
    requests = [
        {"turn_id": turn.id, "text": redact_customer_text(turn.text), "created_at": turn.created_at}
        for turn in turns if turn.speaker == Speaker.CUSTOMER
    ]
    proposal = None
    if call.current_proposal_id:
        record = db.get(PlanProposalRecord, call.current_proposal_id)
        if record:
            decision = calls._decision(record.id)
            source_ids = json.loads(record.source_turn_ids_json)
            operator_source = any(
                by_id[source].speaker == Speaker.SYSTEM
                and "operator" in json.loads(by_id[source].structured_payload_json)
                for source in source_ids if source in by_id
            )
            proposal = {
                "id": record.id,
                "plan": _facts(PlanProposal.model_validate_json(record.proposal_json)),
                "source": "operator" if operator_source else "customer",
                "policy": {
                    "valid": decision.valid,
                    "violations": json.loads(decision.violations_json),
                    "allowed_alternatives": json.loads(decision.allowed_alternatives_json),
                },
                "customer_confirmed": call.customer_confirmed,
            }
    commit = db.exec(
        select(DeliveryCommit).where(DeliveryCommit.session_id == call.id)
        .order_by(DeliveryCommit.committed_at.desc())
    ).first()
    events = db.exec(
        select(AuditEvent).where(
            AuditEvent.session_id == call.id,
            AuditEvent.event_type.in_(["OPERATOR_PLAN_REJECTED", "OPERATOR_PLAN_REVISED"]),
        ).order_by(AuditEvent.id)
    ).all()
    operator_events = []
    for event in events:
        data = redact_payload(json.loads(event.payload_json))
        operator_events.append({
            "id": event.id, "action": "REVISE" if event.event_type.endswith("REVISED") else "REJECT",
            "reason": data.get("reason"), "proposal_id": data.get("proposal_id"),
            "previous_proposal_id": data.get("previous_proposal_id"), "created_at": event.created_at,
        })
    mutable = call.state == CallState.PENDING_CONFIRMATION and proposal is not None and commit is None
    if commit:
        status, text = "COMMITTED", "The confirmed plan has been committed. This does not mean the parcel was delivered."
    elif call.state == CallState.CANCELLED:
        status, text = "CANCELLED", "The customer requested cancellation before dispatch. The order is cancelled and no delivery plan was committed."
    elif call.state in TERMINAL_STATES:
        status, text = "TERMINAL", "The conversation has ended without a committed delivery plan."
    elif call.customer_confirmed:
        status, text = "CONFIRMED", "The customer confirmed the current plan. It has not been committed."
    elif proposal:
        status, text = "PENDING_CONFIRMATION", "The current proposal has not been confirmed by the customer or committed."
    else:
        status, text = "NO_PROPOSAL", "No current delivery proposal is available. Nothing has been committed."
    return {
        "session_id": call.id, "order_id": call.order_id, "state": call.state.value,
        "outcome": call.outcome.value if call.outcome else None,
        "transport": runtime.transport if runtime else "browser",
        "dispatch_state": binding.dispatch_state if binding else None,
        "phone_consent": binding.consented if binding else False,
        "customer_requests": requests, "proposal": proposal,
        "confirmation": {"confirmed": call.customer_confirmed,
            "presentation_pending": pending_operator_revision(db, call.id, call.current_proposal_id),
            "requires_fresh_confirmation": not call.customer_confirmed and call.state not in TERMINAL_STATES and bool(proposal or operator_events)},
        "courier_card": redact_payload(json.loads(commit.courier_card_json)) if commit else None,
        "operator_actions": {"can_reject": mutable, "can_revise": mutable,
            "reason": None if mutable else "Only an uncommitted pending proposal can be changed."},
        "operator_events": operator_events, "summary": {"status": status, "text": text},
    }


def _pending(calls: CallService, session_id: str, expected_id: str):
    call = calls.get_call(session_id)
    committed = calls.db.exec(select(DeliveryCommit.id).where(DeliveryCommit.session_id == call.id)).first()
    if committed or call.state in TERMINAL_STATES:
        raise DomainError(409, "A committed or ended conversation cannot be changed")
    if call.current_proposal_id != expected_id:
        raise DomainError(409, "The proposal changed. Refresh before reviewing it again")
    if call.state != CallState.PENDING_CONFIRMATION:
        raise DomainError(409, "Only a pending proposal can be changed")
    return call


@router.get("/sessions/{session_id}/operator-summary")
def summary(session_id: str, db: SessionDep):
    with ADMISSION_LOCK:
        db.expire_all()
        return operator_summary(db, session_id)


@router.post("/sessions/{session_id}/operator/reject")
def reject(session_id: str, request: OperatorChange, db: SessionDep):
    with ADMISSION_LOCK:
        db.expire_all()
        calls = CallService(db)
        call = _pending(calls, session_id, request.expected_proposal_id)
        reason = redact_customer_text(request.reason)
        payload = {"actor": "operator", "reason": reason, "previous_proposal_id": call.current_proposal_id,
                   "proposal_id": None, "confirmation_invalidated": True}
        db.add(Turn(session_id=call.id, speaker=Speaker.SYSTEM,
            text=f"Operator rejected the pending proposal: {reason}",
            structured_payload_json=json.dumps({"operator": {"action": "REJECT", **payload}}), created_at=calls.clock()))
        call.current_proposal_id = None
        call.customer_confirmed = False
        calls._transition(call, CallState.NEGOTIATION)
        calls._audit(call.id, "OPERATOR_PLAN_REJECTED", payload)
        db.commit()
        return operator_summary(db, call.id)


@router.post("/sessions/{session_id}/operator/revise")
def revise(session_id: str, request: OperatorRevision, db: SessionDep):
    with ADMISSION_LOCK:
        db.expire_all()
        calls = CallService(db)
        call = _pending(calls, session_id, request.expected_proposal_id)
        original_record = db.get(PlanProposalRecord, call.current_proposal_id)
        original = (
            PlanProposal.model_validate_json(original_record.proposal_json)
            if original_record
            else None
        )
        proposal = _operator_facts(request.proposal, original)
        order = calls._order(call.order_id)
        slots = db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order.id)).all()
        verdict = validate_plan(proposal, order, list(slots))
        if not verdict.valid:
            codes = ", ".join(violation.code.value for violation in verdict.violations)
            raise DomainError(409, f"The revised plan violates delivery policy: {codes}")
        reason = redact_customer_text(request.reason)
        proposal_id = uuid4().hex
        payload = {"actor": "operator", "reason": reason, "previous_proposal_id": call.current_proposal_id,
                   "proposal_id": proposal_id, "confirmation_invalidated": True}
        turn = Turn(session_id=call.id, speaker=Speaker.SYSTEM,
            text=f"Operator revised the pending proposal. Fresh customer confirmation is required: {reason}",
            structured_payload_json=json.dumps({"operator": {"action": "REVISE", **payload}}), created_at=calls.clock())
        db.add(turn)
        db.flush()
        db.add(PlanProposalRecord(id=proposal_id, session_id=call.id, proposal_json=proposal.model_dump_json(),
            source_turn_ids_json=json.dumps([turn.id]), created_at=calls.clock()))
        db.add(PolicyDecisionRecord(id=uuid4().hex, proposal_id=proposal_id, valid=True,
            violations_json="[]", allowed_alternatives_json=json.dumps([action.value for action in verdict.allowed_alternatives]), created_at=calls.clock()))
        call.current_proposal_id = proposal_id
        call.customer_confirmed = False
        calls._audit(call.id, "PLAN_VALIDATED", {"actor": "operator", "proposal_id": proposal_id, "valid": True})
        calls._audit(call.id, "OPERATOR_PLAN_REVISED", payload)
        db.commit()
        return operator_summary(db, call.id)
