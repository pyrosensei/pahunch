import json
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlmodel import Session, select

from app.calls import ADMISSION_LOCK, CallService, utc_now
from app.db import get_session
from app.errors import DomainError
from app.language import redact_customer_text, redact_payload
from app.models import (
    CallRuntime,
    CallSession,
    DeliveryCommit,
    DeliverySlot,
    DialerEvent,
    Order,
    OrderStatus,
    PaymentMode,
    PlanProposalRecord,
    PolicyDecisionRecord,
    Turn,
    TwilioBinding,
)
from app.state_machine import TERMINAL_STATES

router = APIRouter(prefix="/api")
SessionDep = Annotated[Session, Depends(get_session)]


class DemoOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_mode: PaymentMode
    parcel_type: Literal["standard", "high_value"] = "standard"
    requires_otp: bool = True


def _now(request: Request):
    dialer = request.app.state.dialer
    return dialer.clock() if dialer is not None else utc_now()


@router.post("/orders", response_model=Order, status_code=201)
def checkout(request: DemoOrderRequest, http_request: Request, db: SessionDep):
    with ADMISSION_LOCK:
        now = _now(http_request)
        order_id = (db.exec(select(func.max(Order.id))).one() or 0) + 1
        start = now + timedelta(minutes=15)
        end = start + timedelta(hours=2)
        order = Order(
            id=order_id,
            merchant_name="SwiftCart",
            delivery_address_summary="Demo Residence, SwiftCart Test Block",
            window_start=start,
            window_end=end,
            payment_mode=request.payment_mode,
            requires_otp=request.requires_otp,
            declared_value_inr=30000 if request.parcel_type == "high_value" else 1800,
            safe_drop_allowed=True,
            neighbour_drop_allowed=True,
            security_drop_allowed=True,
            status=OrderStatus.PENDING,
        )
        db.add(order)
        db.flush()
        for offset in (timedelta(), timedelta(days=1)):
            db.add(DeliverySlot(order_id=order_id, start_at=start + offset, end_at=end + offset))
        db.add(
            DialerEvent(
                order_id=order_id,
                event_type="ORDER_CREATED",
                reason="DEMO_CHECKOUT",
                created_at=now,
            )
        )
        db.commit()
        db.refresh(order)
        return order


@router.post("/orders/{order_id}/out-for-delivery", response_model=Order)
def mark_ready(order_id: int, request: Request, db: SessionDep):
    with ADMISSION_LOCK:
        db.expire_all()
        order = db.get(Order, order_id)
        if order is None:
            raise DomainError(404, "Order not found")
        created = db.exec(
            select(DialerEvent.id).where(
                DialerEvent.order_id == order_id, DialerEvent.event_type == "ORDER_CREATED"
            )
        ).first()
        if created is None or order.status not in {
            OrderStatus.PENDING,
            OrderStatus.OUT_FOR_DELIVERY,
        }:
            raise DomainError(409, "Only a pending synthetic checkout order can be marked ready")
        if order.status == OrderStatus.PENDING:
            order.status = OrderStatus.OUT_FOR_DELIVERY
            db.add(
                DialerEvent(
                    order_id=order_id,
                    event_type="ORDER_READY",
                    reason="DEMO_READY",
                    created_at=_now(request),
                )
            )
            db.commit()
            db.refresh(order)
        return order


@router.get("/sessions")
def sessions(
    db: SessionDep,
    order_id: int | None = None,
    active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    statement = select(CallSession)
    if order_id is not None:
        statement = statement.where(CallSession.order_id == order_id)
    if active is not None:
        statement = statement.where(
            CallSession.state.notin_(TERMINAL_STATES)
            if active
            else CallSession.state.in_(TERMINAL_STATES)
        )
    calls = db.exec(
        statement.order_by(CallSession.started_at.desc(), CallSession.id.desc()).limit(limit)
    ).all()
    result = []
    for call in calls:
        summary = call.model_dump(mode="json")
        runtime = db.get(CallRuntime, call.id)
        summary["transport"] = runtime.transport if runtime else "browser"
        binding = db.get(TwilioBinding, call.id)
        summary["dispatch_state"] = binding.dispatch_state if binding else None
        summary["phone_consent"] = binding.consented if binding else False
        summary["proposal"] = None
        summary["verdict"] = None
        summary["courier_card"] = None
        if call.current_proposal_id:
            proposal = db.get(PlanProposalRecord, call.current_proposal_id)
            decision = db.exec(
                select(PolicyDecisionRecord)
                .where(PolicyDecisionRecord.proposal_id == call.current_proposal_id)
                .order_by(PolicyDecisionRecord.created_at.desc())
            ).first()
            if proposal:
                summary["proposal"] = redact_payload(json.loads(proposal.proposal_json))
            if decision:
                summary["verdict"] = {
                    "valid": decision.valid,
                    "violations": json.loads(decision.violations_json),
                    "allowed_alternatives": json.loads(decision.allowed_alternatives_json),
                }
        commit = db.exec(
            select(DeliveryCommit)
            .where(DeliveryCommit.session_id == call.id)
            .order_by(DeliveryCommit.committed_at.desc())
        ).first()
        if commit:
            summary["courier_card"] = redact_payload(json.loads(commit.courier_card_json))
        result.append(summary)
    return result


@router.get("/sessions/{session_id}/turns")
def transcript(
    session_id: str,
    db: SessionDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    CallService(db).get_call(session_id)
    turns = db.exec(
        select(Turn)
        .where(Turn.session_id == session_id, Turn.id > after_id)
        .order_by(Turn.id)
        .limit(limit)
    ).all()
    return [
        {
            "id": turn.id,
            "speaker": turn.speaker.value,
            "text": redact_customer_text(turn.text),
            "created_at": turn.created_at,
            "structured_payload": redact_payload(json.loads(turn.structured_payload_json)),
        }
        for turn in turns
    ]


@router.get("/dialer/events", response_model=list[DialerEvent])
def dialer_events(
    db: SessionDep, order_id: int | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50
):
    statement = select(DialerEvent)
    if order_id is not None:
        statement = statement.where(DialerEvent.order_id == order_id)
    return db.exec(statement.order_by(DialerEvent.id.desc()).limit(limit)).all()
