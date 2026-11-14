from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanAction(str, Enum):
    HAND_TO_CUSTOMER = "HAND_TO_CUSTOMER"
    LEAVE_WITH_SECURITY = "LEAVE_WITH_SECURITY"
    LEAVE_WITH_NEIGHBOR = "LEAVE_WITH_NEIGHBOR"
    RESCHEDULE = "RESCHEDULE"
    RETRY_AFTER_DELAY = "RETRY_AFTER_DELAY"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"


class PlannerAction(str, Enum):
    RESPOND = "RESPOND"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"
    PROPOSE_PLAN = "PROPOSE_PLAN"
    CONFIRM_PLAN = "CONFIRM_PLAN"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"


class TurnRelevance(str, Enum):
    DELIVERY = "DELIVERY"
    OFF_TOPIC = "OFF_TOPIC"
    MIXED = "MIXED"
    AMBIGUOUS = "AMBIGUOUS"
    CALL_CONTROL = "CALL_CONTROL"


SLOT_ACTIONS = {
    PlanAction.HAND_TO_CUSTOMER,
    PlanAction.LEAVE_WITH_SECURITY,
    PlanAction.LEAVE_WITH_NEIGHBOR,
    PlanAction.RESCHEDULE,
}


class CustomerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: int | None = Field(
        default=None,
        description="Use null for the current customer utterance. Historical IDs must identify a CUSTOMER turn in this session, never an agent or system turn.",
    )
    quote: str = Field(
        min_length=1, max_length=2_000,
        description="Exact verbatim substring of that customer turn in its original script. Preserve spelling. Use null for unused evidence fields, never invent or translate a quote.",
    )


class RequestDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_start: datetime | None = None
    window_end: datetime | None = None
    time_source: CustomerEvidence | None = None
    relative_day_source: CustomerEvidence | None = Field(default=None, description="Quote only today, tomorrow, aaj, kal, आज or कल when the requested primary date is relative to call_date. The backend resolves this day.")
    recipient_source: CustomerEvidence | None = None
    recipient_address_source: CustomerEvidence | None = None
    action: Literal["UNSPECIFIED", "HAND_TO_CUSTOMER", "LEAVE_WITH_NEIGHBOR", "LEAVE_WITH_SECURITY", "RESCHEDULE"] = "UNSPECIFIED"
    handoff_action: Literal["HAND_TO_CUSTOMER", "LEAVE_WITH_NEIGHBOR", "LEAVE_WITH_SECURITY"] | None = Field(default=None, description="The customer's explicitly requested recipient action, independent of a time change. Use HAND_TO_CUSTOMER for 'directly to me instead', even if action is RESCHEDULE. Use null for a time-only change.")
    call_on_arrival: bool = True


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PlanAction
    window_start: datetime | None = None
    window_end: datetime | None = None
    call_on_arrival: bool = True
    delay_minutes: int | None = Field(default=None, ge=1, le=120)
    max_retries: int = Field(default=0, ge=0, le=2)
    named_recipient: str | None = Field(default=None, max_length=80)
    recipient_address: str | None = Field(default=None, max_length=80)
    recipient_source: CustomerEvidence | None = None
    recipient_address_source: CustomerEvidence | None = None
    notes: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action in SLOT_ACTIONS:
            if self.window_start is None or self.window_end is None:
                raise ValueError("A scheduled action requires window_start and window_end")
            if self.window_end <= self.window_start:
                raise ValueError("window_end must be after window_start")
        if self.action == PlanAction.RETRY_AFTER_DELAY and (
            self.delay_minutes is None or self.max_retries < 1
        ):
            raise ValueError("A retry requires delay_minutes and at least one retry")
        if self.named_recipient is not None:
            self.named_recipient = self.named_recipient.strip() or None
        return self


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: PlanStep
    fallback: PlanStep | None = None
    customer_statements: list[str] = Field(default_factory=list, max_length=8)
    notes: str | None = Field(default=None, max_length=500)
    confidence: dict[str, float] = Field(default_factory=dict)


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PlannerAction
    relevance: TurnRelevance = TurnRelevance.DELIVERY
    delivery_quotes: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="For MIXED speech, exact current-utterance substrings directed to this delivery assistant. Exclude household/background speech, quotations and instructions to other people.",
    )
    awaiting_continuation: bool = False
    request_details: RequestDetails | None = None
    missing_fields: list[Literal["time", "end_time", "recipient", "recipient_address", "continuation", "availability", "other"]] = Field(default_factory=list, max_length=6)
    message: str = Field(max_length=500)
    proposal: PlanProposal | None
    escalation_reason: str | None = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.awaiting_continuation and self.action != PlannerAction.ASK_CLARIFICATION:
            raise ValueError("Only a clarification may wait for continued speech")
        if self.action == PlannerAction.PROPOSE_PLAN and self.proposal is None:
            raise ValueError("PROPOSE_PLAN requires a proposal")
        if self.action != PlannerAction.PROPOSE_PLAN and self.proposal is not None:
            raise ValueError("Only PROPOSE_PLAN may include a proposal")
        if self.action == PlannerAction.ESCALATE_TO_HUMAN and not self.escalation_reason:
            raise ValueError("ESCALATE_TO_HUMAN requires an escalation reason")
        if self.action != PlannerAction.ESCALATE_TO_HUMAN and self.escalation_reason is not None:
            raise ValueError("Only ESCALATE_TO_HUMAN may include an escalation reason")
        return self


class PlanValidationRequest(BaseModel):
    proposal: PlanProposal


class ViolationCode(str, Enum):
    OTP_REQUIRED_NO_THIRD_PARTY = "OTP_REQUIRED_NO_THIRD_PARTY"
    COD_REQUIRES_DIRECT_HANDOFF = "COD_REQUIRES_DIRECT_HANDOFF"
    HIGH_VALUE_DIRECT_HANDOFF = "HIGH_VALUE_DIRECT_HANDOFF"
    SLOT_NOT_AVAILABLE = "SLOT_NOT_AVAILABLE"
    SECURITY_DROP_NOT_ALLOWED = "SECURITY_DROP_NOT_ALLOWED"
    NEIGHBOUR_DROP_NOT_ALLOWED = "NEIGHBOUR_DROP_NOT_ALLOWED"
    NAMED_RECIPIENT_REQUIRED = "NAMED_RECIPIENT_REQUIRED"
    RECIPIENT_LOCATION_REQUIRED = "RECIPIENT_LOCATION_REQUIRED"
    MISSING_ORDER_METADATA = "MISSING_ORDER_METADATA"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"


class PolicyViolation(BaseModel):
    code: ViolationCode
    message: str
    path: str


class PolicyVerdict(BaseModel):
    valid: bool
    violations: list[PolicyViolation]
    allowed_alternatives: list[PlanAction]


class CustomerTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    transcript_confidence: float | None = Field(default=None, ge=0, le=1)
    proposal: PlanProposal | None = None
    confirmation: bool = False
    address_change_requested: bool = False
    escalation_reason: str | None = Field(default=None, max_length=300)


class TurnResult(BaseModel):
    session_id: str
    turn_id: int
    state: str
    turn_count: int
    clarification_count: int
    customer_confirmed: bool
    verdict: PolicyVerdict | None = None
    outcome: str | None = None


class AgentTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    transcript_confidence: float | None = Field(default=None, ge=0, le=1)


class PlannerMeta(BaseModel):
    relevance: TurnRelevance = TurnRelevance.DELIVERY
    delivery_quotes: list[str] = Field(default_factory=list)
    presented_proposal_id: str | None = None
    language_warning: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    review_needed: bool = False
    awaiting_continuation: bool = False
    model: str
    action: PlannerAction | None = None
    confidence: float | None = None
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    degraded: bool = False
    error_code: str | None = None
    response_language: Literal["en-IN", "hi-IN", "hi-en"] = "hi-en"
    wording_source: Literal["model", "template"] = "template"
    fallback_reason: str | None = None
    operator_revision_id: str | None = None


class AgentTurnResponse(BaseModel):
    session_id: str
    state: str
    agent_message: str
    turn: TurnResult
    planner: PlannerMeta


class VoiceTimings(BaseModel):
    stt_ms: int = 0
    planner_ms: int = 0
    tts_ms: int = 0
    total_ms: int = 0
    first_audio_ms: int = 0
    first_response_audio_ms: int | None = None


class VoiceTurnResponse(BaseModel):
    session_id: str
    state: str
    transcript: str
    detected_language: str | None = None
    transcript_confidence: float | None = None
    agent_message: str
    turn: TurnResult
    planner: PlannerMeta
    audio_base64: str | None = None
    audio_content_type: str | None = None
    audio_error: str | None = None
    audio_streamed: bool = False
    ack_played: bool = False
    timings: VoiceTimings


class CommitRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


class CourierCard(BaseModel):
    order_id: int
    action: PlanAction
    window_start: datetime | None
    window_end: datetime | None
    call_on_arrival: bool
    named_recipient: str | None = None
    recipient_address: str | None = None
    instructions: list[str]
    generated_at: datetime


class CommitResult(BaseModel):
    commit_id: str
    state: str
    idempotency_key: str
    courier_card: CourierCard
    reused: bool = False
