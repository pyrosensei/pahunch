import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.errors import DomainError
from app.models import (
    AuditEvent,
    CallOutcome,
    CallSession,
    CallState,
    DeliveryCommit,
    DeliverySlot,
    Order,
    OrderStatus,
    PaymentMode,
    PlanProposalRecord,
    PolicyDecisionRecord,
    Speaker,
    Turn,
)
from app.policy import HIGH_VALUE_THRESHOLD_INR, validate_plan
from app.schemas import (
    CommitResult,
    CourierCard,
    CustomerTurnRequest,
    PlanAction,
    PlanProposal,
    TurnResult,
)
from app.state_machine import TERMINAL_STATES, transition
from app.tools import ToolName, require_tool

MAX_TURNS = 8
MAX_CLARIFICATIONS = 3
CALL_COOLDOWN = timedelta(minutes=30)


def utc_now() -> datetime:
    return datetime.now(UTC)


class CallService:
    def __init__(self, db: Session):
        self.db = db

    def _order(self, order_id: int) -> Order:
        order = self.db.get(Order, order_id)
        if order is None:
            raise DomainError(404, "Order not found")
        return order

    def get_call(self, session_id: str) -> CallSession:
        call = self.db.get(CallSession, session_id)
        if call is None:
            raise DomainError(404, "Call session not found")
        return call

    def _audit(self, session_id: str, event_type: str, payload: dict | None = None) -> None:
        self.db.add(
            AuditEvent(
                session_id=session_id,
                event_type=event_type,
                payload_json=json.dumps(payload or {}, default=str),
                created_at=utc_now(),
            )
        )

    def _transition(self, call: CallSession, target: CallState) -> None:
        previous = call.state
        transition(call, target)
        self._audit(call.id, "STATE_CHANGED", {"from": previous, "to": target})

    def _check_eligibility(self, order: Order) -> None:
        if order.status in {OrderStatus.DELIVERED, OrderStatus.CANCELLED}:
            raise DomainError(409, "Delivered or cancelled orders are not callable")
        if order.call_opt_out:
            raise DomainError(409, "Customer has opted out of automated calls")
        if not order.delivery_address_summary.strip():
            raise DomainError(409, "Order metadata is incomplete; human review required")
        if order.last_call_completed_at is not None:
            completed = order.last_call_completed_at
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=UTC)
            if utc_now() - completed < CALL_COOLDOWN:
                raise DomainError(409, "Call cooldown is active")

        existing = self.db.exec(select(CallSession).where(CallSession.order_id == order.id)).all()
        if any(call.state not in TERMINAL_STATES for call in existing):
            raise DomainError(409, "An active call already exists for this order")

    def _trigger_reason(self, order: Order) -> str:
        if order.prior_attempts > 0:
            return "PRIOR_FAILED_ATTEMPT"
        if order.payment_mode == PaymentMode.COD:
            return "COD_ORDER"
        if order.requires_otp:
            return "OTP_VERIFIED_DELIVERY"
        if order.declared_value_inr >= HIGH_VALUE_THRESHOLD_INR:
            return "HIGH_VALUE_DELIVERY"
        return "DELIVERY_CONFIRMATION"

    def active_call(self, order_id: int) -> CallSession | None:
        """The call still in progress for an order, if any."""
        calls = self.db.exec(
            select(CallSession)
            .where(CallSession.order_id == order_id)
            .order_by(CallSession.started_at.desc())
        ).all()
        return next(
            (call for call in calls if call.state not in TERMINAL_STATES), None
        )

    def create_call(self, order_id: int) -> CallSession:
        order = self._order(order_id)
        self._check_eligibility(order)
        call = CallSession(
            id=uuid4().hex,
            order_id=order.id,
            trigger_reason=self._trigger_reason(order),
            state=CallState.CREATED,
            started_at=utc_now(),
        )
        self.db.add(call)
        self._audit(call.id, "CALL_CREATED", {"order_id": order.id})
        self._transition(call, CallState.CALLING)
        self.db.commit()
        self.db.refresh(call)
        return call

    def connect(self, session_id: str) -> CallSession:
        call = self.get_call(session_id)
        self._transition(call, CallState.CONNECTED)
        self.db.commit()
        self.db.refresh(call)
        return call

    def confirm_context(self, session_id: str) -> CallSession:
        call = self.get_call(session_id)
        self._transition(call, CallState.CONTEXT_CONFIRMATION)
        self._transition(call, CallState.DISCOVERY)
        self.db.commit()
        self.db.refresh(call)
        return call

    def mark_no_answer(self, session_id: str) -> CallSession:
        call = self.get_call(session_id)
        self._transition(call, CallState.NO_ANSWER)
        call.outcome = CallOutcome.NO_ANSWER
        call.ended_at = utc_now()
        self._order(call.order_id).last_call_completed_at = call.ended_at
        self.db.commit()
        self.db.refresh(call)
        return call

    def _escalate(self, call: CallSession, reason: str) -> None:
        self._transition(call, CallState.ESCALATED)
        call.outcome = CallOutcome.ESCALATED
        call.ended_at = utc_now()
        self._order(call.order_id).last_call_completed_at = call.ended_at
        self._audit(call.id, "ESCALATED", {"reason": reason})

    def _decision(self, proposal_id: str) -> PolicyDecisionRecord:
        decision = self.db.exec(
            select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.proposal_id == proposal_id)
            .order_by(PolicyDecisionRecord.created_at.desc())
        ).first()
        if decision is None:
            raise DomainError(409, "Proposal has no policy decision")
        return decision

    def submit_turn(self, session_id: str, request: CustomerTurnRequest) -> TurnResult:
        call = self.get_call(session_id)
        if call.state in TERMINAL_STATES:
            raise DomainError(409, "Call session is already terminal")

        turn = Turn(
            session_id=call.id,
            speaker=Speaker.CUSTOMER,
            text=request.text,
            structured_payload_json=request.model_dump_json(),
            created_at=utc_now(),
        )
        self.db.add(turn)
        self.db.flush()
        call.turn_count += 1
        verdict = None

        if call.turn_count > MAX_TURNS:
            self._transition(call, CallState.TURN_BUDGET_EXHAUSTED)
            call.outcome = CallOutcome.TURN_BUDGET_EXHAUSTED
            call.ended_at = utc_now()
        elif request.address_change_requested:
            self._escalate(call, "ADDRESS_CHANGE_REQUESTED")
        elif request.escalation_reason:
            self._escalate(call, request.escalation_reason)
        else:
            if request.proposal is not None:
                require_tool(call.state, ToolName.PROPOSE_PLAN)
                order = self._order(call.order_id)
                slots = list(
                    self.db.exec(
                        select(DeliverySlot).where(DeliverySlot.order_id == order.id)
                    ).all()
                )
                verdict = validate_plan(request.proposal, order, slots)
                proposal_record = PlanProposalRecord(
                    id=uuid4().hex,
                    session_id=call.id,
                    proposal_json=request.proposal.model_dump_json(),
                    source_turn_ids_json=json.dumps([turn.id]),
                    created_at=utc_now(),
                )
                self.db.add(proposal_record)
                self.db.flush()
                self.db.add(
                    PolicyDecisionRecord(
                        id=uuid4().hex,
                        proposal_id=proposal_record.id,
                        valid=verdict.valid,
                        violations_json=json.dumps(
                            [item.model_dump(mode="json") for item in verdict.violations]
                        ),
                        allowed_alternatives_json=json.dumps(
                            [item.value for item in verdict.allowed_alternatives]
                        ),
                        created_at=utc_now(),
                    )
                )
                call.current_proposal_id = proposal_record.id
                call.customer_confirmed = False
                self._audit(
                    call.id,
                    "PLAN_VALIDATED",
                    {"proposal_id": proposal_record.id, "valid": verdict.valid},
                )

                if verdict.valid:
                    if call.state != CallState.PENDING_CONFIRMATION:
                        self._transition(call, CallState.PENDING_CONFIRMATION)
                else:
                    call.clarification_count += 1
                    if call.clarification_count > MAX_CLARIFICATIONS:
                        self._escalate(call, "CLARIFICATION_LIMIT_EXCEEDED")
                    elif call.state != CallState.NEGOTIATION:
                        self._transition(call, CallState.NEGOTIATION)

            if request.confirmation:
                if call.state != CallState.PENDING_CONFIRMATION or not call.current_proposal_id:
                    raise DomainError(409, "No valid plan is awaiting confirmation")
                if not self._decision(call.current_proposal_id).valid:
                    raise DomainError(409, "An invalid plan cannot be confirmed")
                call.customer_confirmed = True
                self._audit(call.id, "CUSTOMER_CONFIRMED")

        self.db.commit()
        return TurnResult(
            session_id=call.id,
            turn_id=turn.id,
            state=call.state.value,
            turn_count=call.turn_count,
            clarification_count=call.clarification_count,
            customer_confirmed=call.customer_confirmed,
            verdict=verdict,
            outcome=call.outcome.value if call.outcome else None,
        )

    def _result(self, commit: DeliveryCommit, *, reused: bool) -> CommitResult:
        return CommitResult(
            commit_id=commit.id,
            state=CallState.COMMITTED.value,
            idempotency_key=commit.idempotency_key,
            courier_card=CourierCard.model_validate_json(commit.courier_card_json),
            reused=reused,
        )

    def commit_plan(self, session_id: str, idempotency_key: str) -> CommitResult:
        call = self.get_call(session_id)
        existing = self.db.exec(
            select(DeliveryCommit).where(DeliveryCommit.idempotency_key == idempotency_key)
        ).first()
        if existing is not None:
            if existing.session_id != session_id or existing.proposal_id != call.current_proposal_id:
                raise DomainError(409, "Idempotency key belongs to a different commit")
            return self._result(existing, reused=True)

        require_tool(call.state, ToolName.COMMIT_PLAN)
        if not call.customer_confirmed:
            raise DomainError(409, "Customer confirmation is required before commit")
        if not call.current_proposal_id:
            raise DomainError(409, "No plan is available to commit")

        proposal_record = self.db.get(PlanProposalRecord, call.current_proposal_id)
        if proposal_record is None or not self._decision(proposal_record.id).valid:
            raise DomainError(409, "Only a policy-valid plan can be committed")

        proposal = PlanProposal.model_validate_json(proposal_record.proposal_json)
        order = self._order(call.order_id)
        slots = list(
            self.db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order.id)).all()
        )
        verdict = validate_plan(proposal, order, slots)
        if not verdict.valid:
            call.customer_confirmed = False
            self._transition(call, CallState.NEGOTIATION)
            self.db.commit()
            raise DomainError(409, "Plan no longer passes current delivery policy")

        now = utc_now()
        card = self._courier_card(order, proposal, now)
        commit = DeliveryCommit(
            id=uuid4().hex,
            session_id=call.id,
            proposal_id=proposal_record.id,
            idempotency_key=idempotency_key,
            plan_json=proposal.model_dump_json(),
            courier_card_json=card.model_dump_json(),
            committed_at=now,
        )
        self.db.add(commit)
        if proposal.primary.action == PlanAction.RESCHEDULE:
            order.status = OrderStatus.RESCHEDULED
        order.last_call_completed_at = now
        self._transition(call, CallState.COMMITTED)
        call.outcome = CallOutcome.COMMITTED
        call.ended_at = now
        self._audit(call.id, "PLAN_COMMITTED", {"commit_id": commit.id})

        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.exec(
                select(DeliveryCommit).where(DeliveryCommit.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                return self._result(existing, reused=True)
            raise
        return self._result(commit, reused=False)

    def _courier_card(self, order: Order, proposal: PlanProposal, now: datetime) -> CourierCard:
        primary = proposal.primary
        instructions = {
            PlanAction.HAND_TO_CUSTOMER: "Direct handoff to customer",
            PlanAction.LEAVE_WITH_SECURITY: "Leave with building security",
            PlanAction.LEAVE_WITH_NEIGHBOR: f"Leave with neighbour: {primary.named_recipient}",
            PlanAction.RESCHEDULE: "Delivery rescheduled to the confirmed window",
            PlanAction.RETRY_AFTER_DELAY: "Retry after the confirmed delay",
            PlanAction.ESCALATE_TO_HUMAN: "Human review required",
        }
        lines = [instructions[primary.action]]
        if primary.call_on_arrival:
            lines.append("Call customer on arrival")
        if order.requires_otp:
            lines.append("Collect OTP; do not leave with security or a neighbour")
        if order.payment_mode == PaymentMode.COD:
            lines.append("Collect cash on delivery")
        if proposal.fallback is not None:
            fallback = proposal.fallback
            if fallback.action == PlanAction.RETRY_AFTER_DELAY:
                lines.append(
                    f"If unreachable, retry after {fallback.delay_minutes} minutes; "
                    f"maximum {fallback.max_retries} retry"
                )
            else:
                lines.append(f"Fallback: {fallback.action.value}")
        return CourierCard(
            order_id=order.id,
            action=primary.action,
            window_start=primary.window_start,
            window_end=primary.window_end,
            call_on_arrival=primary.call_on_arrival,
            instructions=lines,
            generated_at=now,
        )

    def get_courier_card(self, session_id: str) -> CourierCard:
        self.get_call(session_id)
        commit = self.db.exec(
            select(DeliveryCommit)
            .where(DeliveryCommit.session_id == session_id)
            .order_by(DeliveryCommit.committed_at.desc())
        ).first()
        if commit is None:
            raise DomainError(404, "No committed plan exists for this call")
        return CourierCard.model_validate_json(commit.courier_card_json)

    def list_events(self, session_id: str) -> list[AuditEvent]:
        self.get_call(session_id)
        return list(
            self.db.exec(
                select(AuditEvent)
                .where(AuditEvent.session_id == session_id)
                .order_by(AuditEvent.id)
            ).all()
        )
