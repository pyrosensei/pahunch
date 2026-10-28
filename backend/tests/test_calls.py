from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.calls import MAX_CLARIFICATIONS, MAX_TURNS, CallService
from app.errors import DomainError
from app.models import CallState, Order
from app.schemas import CustomerTurnRequest, PlanAction, PlanProposal, PlanStep
from app.seed import seed_database

START = datetime.fromisoformat("2026-11-05T18:00:00+05:30")
END = datetime.fromisoformat("2026-11-05T20:00:00+05:30")


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session)
        yield session


def start_call(service: CallService, order_id: int = 1042):
    call = service.create_call(order_id)
    service.connect(call.id)
    return service.confirm_context(call.id)


def plan(fallback: PlanStep | None = None, action: PlanAction = PlanAction.HAND_TO_CUSTOMER):
    return PlanProposal(
        primary=PlanStep(action=action, window_start=START, window_end=END),
        fallback=fallback,
    )


def submit_plan(service: CallService, session_id: str, proposal: PlanProposal):
    return service.submit_turn(
        session_id,
        CustomerTurnRequest(text="customer plan", proposal=proposal),
    )


def test_full_negotiation_confirmation_and_idempotent_commit(db):
    service = CallService(db)
    call = start_call(service)
    security = PlanStep(
        action=PlanAction.LEAVE_WITH_SECURITY,
        window_start=START,
        window_end=END,
    )
    invalid = submit_plan(service, call.id, plan(fallback=security))
    assert invalid.verdict is not None and not invalid.verdict.valid
    assert invalid.state == CallState.NEGOTIATION.value

    retry = PlanStep(action=PlanAction.RETRY_AFTER_DELAY, delay_minutes=15, max_retries=1)
    valid = submit_plan(service, call.id, plan(fallback=retry))
    assert valid.verdict is not None and valid.verdict.valid
    assert valid.state == CallState.PENDING_CONFIRMATION.value

    with pytest.raises(DomainError, match="confirmation"):
        service.commit_plan(call.id, "hero-commit-0001")

    confirmed = service.submit_turn(
        call.id,
        CustomerTurnRequest(text="haan, confirm", confirmation=True),
    )
    assert confirmed.customer_confirmed

    committed = service.commit_plan(call.id, "hero-commit-0001")
    assert committed.state == CallState.COMMITTED.value
    assert committed.reused is False
    assert any("OTP" in line for line in committed.courier_card.instructions)
    assert any("retry after 15 minutes" in line for line in committed.courier_card.instructions)

    replay = service.commit_plan(call.id, "hero-commit-0001")
    assert replay.reused is True
    assert replay.commit_id == committed.commit_id
    assert service.get_courier_card(call.id).order_id == 1042
    assert any(event.event_type == "PLAN_COMMITTED" for event in service.list_events(call.id))


def test_commit_is_not_available_during_discovery(db):
    service = CallService(db)
    call = start_call(service)
    with pytest.raises(DomainError, match="not allowed"):
        service.commit_plan(call.id, "early-commit-01")


def test_active_call_cannot_be_duplicated(db):
    service = CallService(db)
    service.create_call(1042)
    with pytest.raises(DomainError, match="active call"):
        service.create_call(1042)


def test_delivered_missing_metadata_and_opted_out_orders_are_not_callable(db):
    service = CallService(db)
    with pytest.raises(DomainError):
        service.create_call(1049)
    with pytest.raises(DomainError):
        service.create_call(1047)

    order = db.get(Order, 1042)
    order.call_opt_out = True
    db.commit()
    with pytest.raises(DomainError, match="opted out"):
        service.create_call(1042)


def test_no_answer_is_terminal(db):
    service = CallService(db)
    call = service.create_call(1042)
    result = service.mark_no_answer(call.id)
    assert result.state == CallState.NO_ANSWER
    assert result.ended_at is not None
    with pytest.raises(DomainError):
        service.connect(call.id)


def test_address_change_escalates_without_modifying_address(db):
    service = CallService(db)
    call = start_call(service)
    original = db.get(Order, 1042).delivery_address_summary
    result = service.submit_turn(
        call.id,
        CustomerTurnRequest(text="new address", address_change_requested=True),
    )
    assert result.state == CallState.ESCALATED.value
    assert db.get(Order, 1042).delivery_address_summary == original


def test_turn_budget_is_enforced(db):
    service = CallService(db)
    call = start_call(service)
    result = None
    for index in range(MAX_TURNS + 1):
        result = service.submit_turn(call.id, CustomerTurnRequest(text=f"turn {index}"))
    assert result is not None
    assert result.state == CallState.TURN_BUDGET_EXHAUSTED.value
    assert result.turn_count == MAX_TURNS + 1


def test_repeated_invalid_plans_escalate_after_clarification_limit(db):
    service = CallService(db)
    call = start_call(service)
    security = PlanStep(
        action=PlanAction.LEAVE_WITH_SECURITY,
        window_start=START,
        window_end=END,
    )
    result = None
    for _ in range(MAX_CLARIFICATIONS + 1):
        result = submit_plan(service, call.id, plan(fallback=security))
    assert result is not None
    assert result.state == CallState.ESCALATED.value
    assert result.clarification_count == MAX_CLARIFICATIONS + 1


def test_courier_card_does_not_exist_before_commit(db):
    service = CallService(db)
    call = start_call(service)
    with pytest.raises(DomainError, match="No committed plan"):
        service.get_courier_card(call.id)
