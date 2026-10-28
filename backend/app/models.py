from datetime import datetime
from enum import Enum

from sqlmodel import Field, SQLModel


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


class DeliverySlot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    start_at: datetime
    end_at: datetime


class CallState(str, Enum):
    CREATED = "CREATED"
    CALLING = "CALLING"
    CONNECTED = "CONNECTED"
    CONTEXT_CONFIRMATION = "CONTEXT_CONFIRMATION"
    DISCOVERY = "DISCOVERY"
    NEGOTIATION = "NEGOTIATION"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    COMMITTED = "COMMITTED"
    NO_ANSWER = "NO_ANSWER"
    UNREACHABLE = "UNREACHABLE"
    ESCALATED = "ESCALATED"
    TOOL_FAILURE = "TOOL_FAILURE"
    TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"


class CallOutcome(str, Enum):
    COMMITTED = "COMMITTED"
    NO_ANSWER = "NO_ANSWER"
    UNREACHABLE = "UNREACHABLE"
    ESCALATED = "ESCALATED"
    TOOL_FAILURE = "TOOL_FAILURE"
    TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"


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
