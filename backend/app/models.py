from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import field_serializer
from sqlmodel import Field, SQLModel

DELIVERY_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


class PaymentMode(str, Enum):
    PREPAID = "PREPAID"
    COD = "COD"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    RESCHEDULED = "RESCHEDULED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class Order(SQLModel, table=True):
    id: int = Field(primary_key=True)
    merchant_name: str
    delivery_address_summary: str
    window_start: datetime
    window_end: datetime
    payment_mode: PaymentMode
    requires_otp: bool = False
    declared_value_inr: int = 0
    safe_drop_allowed: bool = False
    neighbour_drop_allowed: bool = False
    security_drop_allowed: bool = False
    prior_attempts: int = 0
    status: OrderStatus = OrderStatus.PENDING
    call_opt_out: bool = False
    last_call_completed_at: datetime | None = None

    @field_serializer("window_start", "window_end", when_used="json")
    def serialize_delivery_window(self, value: datetime) -> datetime:
        # SQLite drops offsets. Order/slot wall times follow the policy's IST
        # convention; audit/session clocks remain UTC and are not changed here.
        return value if value.tzinfo else value.replace(tzinfo=DELIVERY_TIMEZONE)


class DemoOrderProduct(SQLModel, table=True):
    order_id: int = Field(primary_key=True, foreign_key="order.id")
    product_id: str
    product_name: str
    price_inr: int


class DeliverySlot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    start_at: datetime
    end_at: datetime

    @field_serializer("start_at", "end_at", when_used="json")
    def serialize_delivery_window(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=DELIVERY_TIMEZONE)


class CallState(str, Enum):
    CREATED = "CREATED"
    CALLING = "CALLING"
    CONNECTED = "CONNECTED"
    CONTEXT_CONFIRMATION = "CONTEXT_CONFIRMATION"
    DISCOVERY = "DISCOVERY"
    NEGOTIATION = "NEGOTIATION"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    COMMITTED = "COMMITTED"
    DECLINED = "DECLINED"
    NO_ANSWER = "NO_ANSWER"
    UNREACHABLE = "UNREACHABLE"
    ESCALATED = "ESCALATED"
    TOOL_FAILURE = "TOOL_FAILURE"
    TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"


class CallOutcome(str, Enum):
    COMMITTED = "COMMITTED"
    DECLINED = "DECLINED"
    NO_ANSWER = "NO_ANSWER"
    UNREACHABLE = "UNREACHABLE"
    ESCALATED = "ESCALATED"
    TOOL_FAILURE = "TOOL_FAILURE"
    TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"


class CallSession(SQLModel, table=True):
    id: str = Field(primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    trigger_reason: str
    state: CallState = Field(index=True)
    started_at: datetime
    ended_at: datetime | None = None
    outcome: CallOutcome | None = None
    turn_count: int = 0
    clarification_count: int = 0
    customer_confirmed: bool = False
    current_proposal_id: str | None = None


class Speaker(str, Enum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class Turn(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="callsession.id", index=True)
    speaker: Speaker
    text: str
    structured_payload_json: str = "{}"
    created_at: datetime


class PlanProposalRecord(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="callsession.id", index=True)
    proposal_json: str
    source_turn_ids_json: str = "[]"
    created_at: datetime


class PolicyDecisionRecord(SQLModel, table=True):
    id: str = Field(primary_key=True)
    proposal_id: str = Field(foreign_key="planproposalrecord.id", index=True)
    valid: bool
    violations_json: str
    allowed_alternatives_json: str
    created_at: datetime


class DeliveryCommit(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="callsession.id", index=True)
    proposal_id: str = Field(foreign_key="planproposalrecord.id")
    idempotency_key: str = Field(unique=True, index=True)
    plan_json: str
    courier_card_json: str
    committed_at: datetime


class AuditEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="callsession.id", index=True)
    event_type: str
    payload_json: str = "{}"
    created_at: datetime


class CallRuntime(SQLModel, table=True):
    session_id: str = Field(primary_key=True, foreign_key="callsession.id")
    transport: str = "browser"
    last_seen_at: datetime
    disconnected_at: datetime | None = None
    observed_state: CallState
    observed_outcome: CallOutcome | None = None
    retryable_failure: bool = False


class DialerOrderState(SQLModel, table=True):
    order_id: int = Field(primary_key=True, foreign_key="order.id")
    skip_reason: str
    session_id: str | None = Field(default=None, foreign_key="callsession.id")
    checked_at: datetime


class DialerEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    session_id: str | None = Field(default=None, foreign_key="callsession.id", index=True)
    event_type: str
    reason: str
    payload_json: str = "{}"
    created_at: datetime


class TwilioBinding(SQLModel, table=True):
    session_id: str = Field(primary_key=True, foreign_key="callsession.id")
    call_sid: str | None = Field(default=None, unique=True, index=True)
    stream_sid: str | None = None
    dispatch_state: str = "REQUESTING"
    provider_status: str = "queued"
    last_callback_sequence: int = -1
    consented: bool = False


class PhoneDemoPermit(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    armed_at: datetime
    expires_at: datetime
    remaining_calls: int = 0
    disarmed: bool = False


class PhoneDemoApproval(SQLModel, table=True):
    session_id: str = Field(primary_key=True, foreign_key="callsession.id")
    approved_at: datetime
