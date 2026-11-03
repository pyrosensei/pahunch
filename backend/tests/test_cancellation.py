import asyncio

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import start_call

from app.calls import CallService
from app.cancellation import handle_cancellation
from app.errors import DomainError
from app.models import AuditEvent, CallState, DeliveryCommit, Order, OrderStatus, Turn
from app.planner import PlannerService
from app.schemas import AgentTurnRequest
from app.state_machine import TERMINAL_STATES

db = planner_database


class NoModel:
    model_name = "unused"

    async def complete_json(self, **kwargs):
        raise AssertionError("Cancellation must use shipment facts, not the model")


def pending_call(db):
    order = db.get(Order, 1042)
    order.status = OrderStatus.PENDING
    db.commit()
    return start_call(CallService(db))


@pytest.mark.parametrize("text,language", [("Please cancel my order", "en-IN"), ("कोई नहीं मेरा ऑर्डर कैंसल कर दीजिए", "hi-IN"), ("कोई नहीं, मेरा order cancel कर दीजिए।", "hi-IN")])
def test_explicit_request_cancels_unshipped_order_and_closes_call(db, text, language):
    call = pending_call(db)
    result, message, meta = handle_cancellation(db, call.id, text, language)
    assert result.state == result.outcome == "CANCELLED"
    assert db.get(Order, 1042).status == OrderStatus.CANCELLED
    assert CallState.CANCELLED in TERMINAL_STATES
    assert not result.customer_confirmed
    assert not db.exec(select(DeliveryCommit)).all()
    assert meta.fallback_reason == "ORDER_CANCELLED"
    assert "cancelled" in message if language == "en-IN" else "रद्द कर दिया" in message
    events = db.exec(select(AuditEvent).where(AuditEvent.event_type == "ORDER_CANCELLED")).all()
    assert len(events) == 1
    with pytest.raises(DomainError):
        handle_cancellation(db, call.id, text, language)
    assert len(db.exec(select(Turn)).all()) == 1


@pytest.mark.parametrize("status", [OrderStatus.OUT_FOR_DELIVERY, OrderStatus.RESCHEDULED, OrderStatus.DELIVERED])
def test_dispatched_or_delivered_order_cannot_be_cancelled(db, status):
    call = start_call(CallService(db))
    order = db.get(Order, 1042)
    order.status = status
    db.commit()
    result, message, meta = handle_cancellation(db, call.id, "cancel my order", "en-IN")
    assert result.state != "CANCELLED"
    assert order.status == status
    assert "cannot cancel" in message
    assert meta.fallback_reason == "CANCELLATION_NOT_AVAILABLE"


@pytest.mark.parametrize("text", ["Don't cancel my order", "मेरा ऑर्डर कैंसल मत करना", "my last order was cancelled", "if I cancel my order what happens?", "Why did you cancel my order?", "My neighbour said cancel my order"])
def test_mentions_and_negations_do_not_cancel(db, text):
    call = pending_call(db)
    assert handle_cancellation(db, call.id, text, "en-IN") is None
    assert db.get(Order, 1042).status == OrderStatus.PENDING
    assert call.turn_count == 0


def test_cancellation_question_is_information_only(db):
    call = pending_call(db)
    result, message, _ = handle_cancellation(db, call.id, "Can I cancel my order?", "en-IN")
    assert db.get(Order, 1042).status == OrderStatus.PENDING
    assert result.state == "DISCOVERY"
    assert "Would you like" in message


def test_planner_routes_request_to_checked_order_action(db):
    call = pending_call(db)
    response = asyncio.run(PlannerService(db, NoModel()).handle_turn(call.id, AgentTurnRequest(text="Please cancel my order")))
    assert response.state == "CANCELLED"
    assert response.planner.fallback_reason == "ORDER_CANCELLED"
    assert response.agent_message.startswith("Your order has been cancelled")
