from datetime import datetime

import pytest
from sqlmodel import delete, select
from test_planner import db as planner_database
from test_planner import decision, run, start_call

from app.availability import expand_demo_availability
from app.calls import CallService, utc_now
from app.errors import DomainError
from app.models import DeliverySlot, DialerEvent, Order
from app.planner import PlannerService
from app.policy import validate_plan
from app.schemas import PlanAction, PlannerAction, PlanProposal, PlanStep

db = planner_database


def at(value):
    return datetime.fromisoformat(f"2026-11-{value}+05:30")


def setup_availability(db):
    order = db.get(Order, 1042)
    order.window_start, order.window_end = at("05T10:00:00"), at("05T12:00:00")
    db.exec(delete(DeliverySlot).where(DeliverySlot.order_id == order.id))
    slots = [DeliverySlot(order_id=order.id, start_at=at(f"{day}T08:00:00"), end_at=at(f"{day}T18:00:00")) for day in ("05", "06")]
    db.add_all(slots)
    db.commit()
    return order, slots


@pytest.mark.parametrize("start,end,valid", [
    ("05T15:00:00", "05T17:00:00", True),
    ("05T08:00:00", "05T10:00:00", False),
    ("05T10:00:00", "05T12:00:00", True),
    ("05T17:00:00", "05T18:00:00", True),
    ("05T17:59:59.999999", "05T18:00:00.000001", False),
    ("06T15:00:00", "06T17:00:00", True),
    ("05T07:00:00", "05T09:00:00", False),
    ("05T17:00:00", "05T19:00:00", False),
    ("04T15:00:00", "04T17:00:00", False),
    ("07T15:00:00", "07T17:00:00", False),
    ("05T17:00:00", "06T09:00:00", False),
])
def test_windows_fit_real_availability_not_exact_pairs(db, start, end, valid):
    order, slots = setup_availability(db)
    proposal = PlanProposal(primary=PlanStep(action=PlanAction.RESCHEDULE, window_start=at(start), window_end=at(end)))
    assert validate_plan(proposal, order, slots).valid is valid


def test_requested_three_pm_requires_confirmation_then_commits(db, scripted_llm):
    order, _ = setup_availability(db)
    call = start_call(CallService(db))
    llm = scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal={"primary": {"action": "RESCHEDULE", "window_start": at("05T15:00:00").isoformat(), "window_end": at("05T17:00:00").isoformat()}})])
    planner = PlannerService(db, llm)
    proposed = run(planner, call.id, "Please move delivery to 3 pm on the same date")
    assert proposed.turn.verdict.valid and not proposed.turn.customer_confirmed
    assert "3 PM-5 PM" in proposed.agent_message
    with pytest.raises(DomainError):
        planner.calls.commit_plan(call.id, "unconfirmed-change")
    confirmed = run(planner, call.id, "Yes, please go ahead")
    assert confirmed.turn.customer_confirmed
    planner.calls.commit_plan(call.id, "confirmed-three-pm")
    db.refresh(order)
    assert order.window_start.astimezone(at("05T15:00:00").tzinfo).hour == 15


@pytest.mark.parametrize("question", ["Which time is available?", "What delivery slots are available?", "कौन सा टाइम उपलब्ध है?", "टाइम चेंज नहीं कर सकते विच टाइम इज़ अवेलेबल"])
def test_availability_questions_do_not_call_model_or_repeat_rejected_plan(db, question):
    setup_availability(db)
    class NoModel:
        model_name = "unused"
        async def complete_json(self, **kwargs):
            raise AssertionError("Availability comes from backend data")
    call = start_call(CallService(db))
    response = run(PlannerService(db, NoModel()), call.id, question)
    assert response.planner.fallback_reason == "DELIVERY_AVAILABILITY"
    if response.planner.response_language == "hi-IN":
        assert "सुबह 10" in response.agent_message and "शाम 6" in response.agent_message
        assert "नवंबर" in response.agent_message
    else:
        assert "10 AM" in response.agent_message and "6 PM" in response.agent_message
        assert "05 November" in response.agent_message and "06 November" in response.agent_message
    assert "08:00" not in response.agent_message and "8 AM" not in response.agent_message
    assert not response.turn.customer_confirmed


def test_out_of_hours_request_offers_actual_dates_and_no_false_reschedule(db, scripted_llm):
    setup_availability(db)
    call = start_call(CallService(db))
    llm = scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal={"primary": {"action": "RESCHEDULE", "window_start": at("05T19:00:00").isoformat(), "window_end": at("05T21:00:00").isoformat()}})])
    response = run(PlannerService(db, llm), call.id, "Please deliver at 7 pm on the same day")
    assert not response.turn.verdict.valid
    assert "06 November" in response.agent_message and "10 AM" in response.agent_message
    assert "6 PM" in response.agent_message
    assert "05 November" not in response.agent_message
    assert not response.turn.customer_confirmed


def test_existing_synthetic_options_expand_idempotently_without_changing_promise(db):
    order = db.get(Order, 1042)
    original = order.window_start
    db.add(DialerEvent(order_id=order.id, event_type="ORDER_CREATED", reason="DEMO_CHECKOUT", created_at=utc_now()))
    db.commit()
    expand_demo_availability(db)
    expand_demo_availability(db)
    slots = db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order.id)).all()
    assert all(s.start_at.astimezone(at("05T10:00:00").tzinfo).hour == 10 for s in slots)
    assert all(s.end_at.astimezone(at("05T10:00:00").tzinfo).hour == 18 for s in slots)
    assert order.window_start == original
