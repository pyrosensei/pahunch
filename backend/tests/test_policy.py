from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models import DeliverySlot, Order, PaymentMode
from app.policy import validate_plan
from app.schemas import PlanAction, PlanProposal, PlanStep, ViolationCode

START = datetime.fromisoformat("2026-11-05T18:00:00+05:30")
END = datetime.fromisoformat("2026-11-05T20:00:00+05:30")


def make_order(**overrides) -> Order:
    values = {
        "id": 1,
        "merchant_name": "SwiftCart",
        "delivery_address_summary": "Flat 1, Test Building",
        "window_start": START,
        "window_end": END,
        "payment_mode": PaymentMode.PREPAID,
        "declared_value_inr": 1_000,
    }
    values.update(overrides)
    return Order(**values)


def make_slot(start: datetime = START, end: datetime = END) -> DeliverySlot:
    return DeliverySlot(order_id=1, start_at=start, end_at=end)


def step(action: PlanAction, **overrides) -> PlanStep:
    values = {"action": action}
    if action in {
        PlanAction.HAND_TO_CUSTOMER,
        PlanAction.LEAVE_WITH_SECURITY,
        PlanAction.LEAVE_WITH_NEIGHBOR,
        PlanAction.RESCHEDULE,
    }:
        values.update(window_start=START, window_end=END)
    if action == PlanAction.RETRY_AFTER_DELAY:
        values.update(delay_minutes=15, max_retries=1)
    values.update(overrides)
    return PlanStep(**values)


def proposal(primary: PlanStep, fallback: PlanStep | None = None) -> PlanProposal:
    return PlanProposal(primary=primary, fallback=fallback)


def codes(verdict) -> set[ViolationCode]:
    return {violation.code for violation in verdict.violations}


def test_direct_handoff_with_available_slot_is_valid():
    verdict = validate_plan(proposal(step(PlanAction.HAND_TO_CUSTOMER)), make_order(), [make_slot()])
    assert verdict.valid
    assert verdict.violations == []


def test_unavailable_slot_is_rejected():
    other_slot = make_slot(
        datetime.fromisoformat("2026-11-06T10:00:00+05:30"),
        datetime.fromisoformat("2026-11-06T12:00:00+05:30"),
    )
    requested = step(
        PlanAction.RESCHEDULE,
        window_start=datetime.fromisoformat("2026-11-07T10:00:00+05:30"),
        window_end=datetime.fromisoformat("2026-11-07T13:00:00+05:30"),
    )
    verdict = validate_plan(proposal(requested), make_order(), [other_slot])
    assert codes(verdict) == {ViolationCode.SLOT_NOT_AVAILABLE}


def test_order_window_is_deliverable_without_a_slot_row():
    other_slot = make_slot(
        datetime.fromisoformat("2026-11-06T10:00:00+05:30"),
        datetime.fromisoformat("2026-11-06T12:00:00+05:30"),
    )
    verdict = validate_plan(
        proposal(step(PlanAction.HAND_TO_CUSTOMER)),
        make_order(),
        [other_slot],
    )
    assert verdict.valid


def test_equivalent_utc_window_matches_ist_slot():
    utc_start = START.astimezone(UTC)
    utc_end = END.astimezone(UTC)
    plan = proposal(step(PlanAction.HAND_TO_CUSTOMER, window_start=utc_start, window_end=utc_end))
    assert validate_plan(plan, make_order(), [make_slot()]).valid


def test_otp_order_rejects_security_fallback():
    order = make_order(requires_otp=True, safe_drop_allowed=True, security_drop_allowed=True)
    plan = proposal(step(PlanAction.HAND_TO_CUSTOMER), step(PlanAction.LEAVE_WITH_SECURITY))
    verdict = validate_plan(plan, order, [make_slot()])
    assert ViolationCode.OTP_REQUIRED_NO_THIRD_PARTY in codes(verdict)
    assert PlanAction.LEAVE_WITH_SECURITY not in verdict.allowed_alternatives


def test_cod_order_rejects_neighbour_handoff():
    order = make_order(
        payment_mode=PaymentMode.COD,
        safe_drop_allowed=True,
        neighbour_drop_allowed=True,
    )
    plan = proposal(step(PlanAction.LEAVE_WITH_NEIGHBOR, named_recipient="Sharma"))
    verdict = validate_plan(plan, order, [make_slot()])
    assert ViolationCode.COD_REQUIRES_DIRECT_HANDOFF in codes(verdict)


def test_high_value_order_requires_direct_handoff():
    order = make_order(
        declared_value_inr=50_000,
        safe_drop_allowed=True,
        security_drop_allowed=True,
    )
    verdict = validate_plan(proposal(step(PlanAction.LEAVE_WITH_SECURITY)), order, [make_slot()])
    assert ViolationCode.HIGH_VALUE_DIRECT_HANDOFF in codes(verdict)


def test_security_drop_requires_order_permission():
    verdict = validate_plan(proposal(step(PlanAction.LEAVE_WITH_SECURITY)), make_order(), [make_slot()])
    assert codes(verdict) == {ViolationCode.SECURITY_DROP_NOT_ALLOWED}


def test_neighbour_drop_requires_permission_and_named_recipient():
    order = make_order(safe_drop_allowed=True, neighbour_drop_allowed=True)
    plan = proposal(step(PlanAction.LEAVE_WITH_NEIGHBOR))
    verdict = validate_plan(plan, order, [make_slot()])
    assert codes(verdict) == {ViolationCode.NAMED_RECIPIENT_REQUIRED}

    valid_plan = proposal(step(PlanAction.LEAVE_WITH_NEIGHBOR, named_recipient="Sharma"))
    assert validate_plan(valid_plan, order, [make_slot()]).valid


def test_retry_does_not_require_a_slot():
    verdict = validate_plan(proposal(step(PlanAction.RETRY_AFTER_DELAY)), make_order(), [])
    assert verdict.valid


def test_missing_address_forces_human_escalation():
    verdict = validate_plan(
        proposal(step(PlanAction.HAND_TO_CUSTOMER)),
        make_order(delivery_address_summary=""),
        [make_slot()],
    )
    assert codes(verdict) == {ViolationCode.MISSING_ORDER_METADATA}
    assert verdict.allowed_alternatives == [PlanAction.ESCALATE_TO_HUMAN]


def test_primary_and_fallback_violations_have_separate_paths():
    order = make_order(requires_otp=True, safe_drop_allowed=True, security_drop_allowed=True)
    plan = proposal(
        step(PlanAction.LEAVE_WITH_SECURITY),
        step(PlanAction.LEAVE_WITH_SECURITY),
    )
    verdict = validate_plan(plan, order, [make_slot()])
    paths = {
        violation.path
        for violation in verdict.violations
        if violation.code == ViolationCode.OTP_REQUIRED_NO_THIRD_PARTY
    }
    assert paths == {"primary", "fallback"}


def test_scheduled_action_requires_window():
    with pytest.raises(ValidationError):
        PlanStep(action=PlanAction.HAND_TO_CUSTOMER)


def test_retry_requires_delay_and_retry_count():
    with pytest.raises(ValidationError):
        PlanStep(action=PlanAction.RETRY_AFTER_DELAY)


def test_escalation_cannot_be_committed_as_a_delivery_plan():
    verdict = validate_plan(
        proposal(step(PlanAction.ESCALATE_TO_HUMAN)),
        make_order(),
        [make_slot()],
    )
    assert not verdict.valid
    assert codes(verdict) == {ViolationCode.ESCALATION_REQUIRED}
    assert verdict.allowed_alternatives == [PlanAction.ESCALATE_TO_HUMAN]
