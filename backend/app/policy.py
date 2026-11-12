import unicodedata
from datetime import UTC, datetime, time, timedelta, timezone

from app.models import DeliverySlot, Order, PaymentMode
from app.schemas import (
    SLOT_ACTIONS,
    PlanAction,
    PlanProposal,
    PlanStep,
    PolicyVerdict,
    PolicyViolation,
    ViolationCode,
)

HIGH_VALUE_THRESHOLD_INR = 25_000
IST = timezone(timedelta(hours=5, minutes=30))
THIRD_PARTY_ACTIONS = {PlanAction.LEAVE_WITH_SECURITY, PlanAction.LEAVE_WITH_NEIGHBOR}

# Bare proximity does not identify premises. This validates an extracted
# location; the planner still interprets the customer's full instructions.
_VAGUE_LOCATIONS = {
    "nearby", "near", "next door", "nextdoor", "beside me", "next to me",
    "near my house", "same building", "neighbour", "neighbor", "not known", "unknown",
    "बगल में", "बगल वाले", "पास में", "पास ही", "पास", "मेरे बगल में",
    "bagal mein", "bagal me", "bagal wale", "paas mein", "paas me", "paas hi",
}


def usable_recipient_location(value: str | None) -> bool:
    normalized = " ".join(unicodedata.normalize("NFKC", value or "").casefold().split()).strip(" .,!?;।")
    return bool(normalized) and normalized not in _VAGUE_LOCATIONS


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(UTC)


def _slot_matches(step: PlanStep, slots: list[DeliverySlot]) -> bool:
    if step.window_start is None or step.window_end is None:
        return False
    return any(
        _utc(slot.start_at) <= _utc(step.window_start)
        and _utc(step.window_end) <= _utc(slot.end_at)
        for slot in slots
    )


def _window_is_deliverable(
    step: PlanStep,
    order: Order,
    slots: list[DeliverySlot],
) -> bool:
    if step.window_start is None or step.window_end is None:
        return False
    # The order's current window is already scheduled even when only
    # reschedule options appear in available_slots.
    if _utc(step.window_start) == _utc(order.window_start) and _utc(
        step.window_end
    ) == _utc(order.window_end):
        return True
    start = _utc(step.window_start).astimezone(IST)
    end = _utc(step.window_end).astimezone(IST)
    # Existing promises above are preserved; every newly requested window
    # must fit the operating day as well as recorded order availability.
    if start.date() != end.date() or start.time() < time(10, 0) or end.time() > time(18, 0):
        return False
    return _slot_matches(step, slots)


def _allowed_alternatives(order: Order) -> list[PlanAction]:
    if not order.delivery_address_summary.strip():
        return [PlanAction.ESCALATE_TO_HUMAN]

    alternatives = [
        PlanAction.HAND_TO_CUSTOMER,
        PlanAction.RESCHEDULE,
        PlanAction.RETRY_AFTER_DELAY,
    ]
    direct_handoff_only = (
        order.payment_mode == PaymentMode.COD
        or order.declared_value_inr >= HIGH_VALUE_THRESHOLD_INR
    )
    if not direct_handoff_only and order.safe_drop_allowed:
        if order.security_drop_allowed and not order.requires_otp:
            alternatives.append(PlanAction.LEAVE_WITH_SECURITY)
        if order.neighbour_drop_allowed:
            alternatives.append(PlanAction.LEAVE_WITH_NEIGHBOR)
    alternatives.append(PlanAction.ESCALATE_TO_HUMAN)
    return alternatives


def validate_plan(
    proposal: PlanProposal,
    order: Order,
    slots: list[DeliverySlot],
) -> PolicyVerdict:
    violations: list[PolicyViolation] = []
    steps = [("primary", proposal.primary)]
    if proposal.fallback is not None:
        steps.append(("fallback", proposal.fallback))

    if not order.delivery_address_summary.strip():
        violations.append(
            PolicyViolation(
                code=ViolationCode.MISSING_ORDER_METADATA,
                message="Delivery address metadata is missing; escalate to a human.",
                path="order",
            )
        )

    requires_escalation = any(step.action == PlanAction.ESCALATE_TO_HUMAN for _, step in steps)
    if requires_escalation:
        violations.append(
            PolicyViolation(
                code=ViolationCode.ESCALATION_REQUIRED,
                message="This request requires human handling, not a delivery plan.",
                path="order",
            )
        )

    for path, step in steps:
        if step.action in SLOT_ACTIONS and not _window_is_deliverable(
            step, order, slots
        ):
            violations.append(
                PolicyViolation(
                    code=ViolationCode.SLOT_NOT_AVAILABLE,
                    message="The requested window is not an available delivery slot.",
                    path=path,
                )
            )

        if step.action in THIRD_PARTY_ACTIONS:
            if order.requires_otp and (step.action != PlanAction.LEAVE_WITH_NEIGHBOR or not step.call_on_arrival):
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.OTP_REQUIRED_NO_THIRD_PARTY,
                        message="OTP delivery requires courier verification with the customer; an authorized neighbour handoff requires a call on arrival.",
                        path=path,
                    )
                )
            if order.payment_mode == PaymentMode.COD:
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.COD_REQUIRES_DIRECT_HANDOFF,
                        message="A cash-on-delivery parcel requires direct handoff to the customer.",
                        path=path,
                    )
                )
            if order.declared_value_inr >= HIGH_VALUE_THRESHOLD_INR:
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.HIGH_VALUE_DIRECT_HANDOFF,
                        message="A high-value parcel requires direct handoff to the customer.",
                        path=path,
                    )
                )

        if step.action == PlanAction.LEAVE_WITH_SECURITY and not (
            order.safe_drop_allowed and order.security_drop_allowed
        ):
            violations.append(
                PolicyViolation(
                    code=ViolationCode.SECURITY_DROP_NOT_ALLOWED,
                    message="This order does not permit delivery to building security.",
                    path=path,
                )
            )

        if step.action == PlanAction.LEAVE_WITH_NEIGHBOR:
            if not (order.safe_drop_allowed and order.neighbour_drop_allowed):
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.NEIGHBOUR_DROP_NOT_ALLOWED,
                        message="This order does not permit delivery to a neighbour.",
                        path=path,
                    )
                )
            if not step.named_recipient:
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.NAMED_RECIPIENT_REQUIRED,
                        message="A neighbour handoff requires a named recipient.",
                        path=path,
                    )
                )
            if not usable_recipient_location(step.recipient_address):
                violations.append(PolicyViolation(
                    code=ViolationCode.RECIPIENT_LOCATION_REQUIRED,
                    message="A neighbour handoff requires a house/flat number or clear finding instructions.",
                    path=path,
                ))

    return PolicyVerdict(
        valid=not violations,
        violations=violations,
        allowed_alternatives=(
            [PlanAction.ESCALATE_TO_HUMAN]
            if requires_escalation
            else _allowed_alternatives(order)
        ),
    )
