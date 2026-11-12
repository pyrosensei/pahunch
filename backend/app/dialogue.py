"""Source-backed request details and recoverable clarification context."""

from datetime import timedelta

from sqlmodel import Session, select

from app.calls import utc_now
from app.language import has_otp_disclosure, recipient_label
from app.models import AuditEvent, CallSession, Order, PlanProposalRecord, Speaker, Turn
from app.policy import IST, _allowed_alternatives, _utc, usable_recipient_location
from app.schemas import (
    CustomerEvidence,
    PlanAction,
    PlannerAction,
    PlanProposal,
    PlanStep,
    RequestDetails,
)


class EvidenceError(ValueError):
    def __init__(self, field: str, reason: str, reference_kind: str, current_match: bool):
        prefix = {
            "time_source": "TIME_SOURCE",
            "recipient_source": "RECIPIENT_SOURCE",
            "recipient_address_source": "RECIPIENT_ADDRESS_SOURCE",
        }[field]
        super().__init__(f"{prefix}_{reason}")
        self.field = field
        self.reference_kind = reference_kind
        self.current_match = current_match


def evidence_text(
    db: Session, session_id: str, current_text: str, evidence: CustomerEvidence,
    *, field: str = "recipient_source",
) -> str:
    source = current_text
    reference_kind = "current"
    current_match = evidence.quote in current_text
    # A verbatim quote in the current utterance is sufficient evidence even
    # when the model guessed a stale turn id. Bind it to the current turn.
    if current_match:
        evidence.turn_id = None
    elif evidence.turn_id is not None:
        turn = db.get(Turn, evidence.turn_id)
        if turn is None:
            reference_kind = "missing"
        elif turn.session_id != session_id:
            reference_kind = "foreign_session"
        else:
            reference_kind = turn.speaker.value.lower()
        if reference_kind != "customer":
            raise EvidenceError(field, "NOT_CUSTOMER", reference_kind, current_match)
        source = turn.text
    if evidence.quote not in source or has_otp_disclosure(evidence.quote):
        raise EvidenceError(field, "MISMATCH", reference_kind, current_match)
    return evidence.quote


def load_details(db: Session, session_id: str) -> RequestDetails:
    event = db.exec(
        select(AuditEvent)
        .where(AuditEvent.session_id == session_id, AuditEvent.event_type == "REQUEST_DETAILS")
        .order_by(AuditEvent.id.desc())
    ).first()
    return RequestDetails.model_validate_json(event.payload_json) if event else RequestDetails()


def effective_preferences(details: RequestDetails) -> tuple[object, ...]:
    # Compare resolved preferences, not evidence references or time-quote wording.
    return (
        _utc(details.window_start) if details.window_start is not None else None,
        _utc(details.window_end) if details.window_end is not None else None,
        details.action,
        details.call_on_arrival,
        details.recipient_source.quote if details.recipient_source is not None else None,
        details.recipient_address_source.quote if details.recipient_address_source is not None else None,
    )


def merge_details(db, session_id, text, old, new):
    if new is None:
        return old.model_copy(deep=True)
    result = old.model_copy(deep=True)
    call = db.get(CallSession, session_id)
    order = db.get(Order, call.order_id)
    recipient_changed = (
        new.recipient_source is not None
        and old.recipient_source is not None
        and new.recipient_source.quote.casefold() != old.recipient_source.quote.casefold()
    )
    if recipient_changed:
        result.recipient_address_source = None
    for field in ("time_source", "recipient_source", "recipient_address_source"):
        evidence = getattr(new, field)
        if evidence is not None:
            if field == "recipient_address_source" and recipient_changed and evidence.quote not in text:
                continue
            evidence_text(db, session_id, text, evidence, field=field)
            if field == "recipient_address_source" and not usable_recipient_location(evidence.quote):
                result.recipient_address_source = None
                continue
            setattr(result, field, evidence)
    if new.window_start is not None:
        if new.time_source is None and _utc(new.window_start) != _utc(order.window_start):
            raise ValueError("TIME_SOURCE_REQUIRED")
        if result.window_start != new.window_start:
            result.window_end = None
        result.window_start = new.window_start
    if new.window_end is not None:
        if new.time_source is None and (
            _utc(new.window_end) != _utc(order.window_end)
            or (new.window_start is not None and _utc(new.window_start) != _utc(order.window_start))
        ):
            raise ValueError("TIME_SOURCE_REQUIRED")
        result.window_end = new.window_end
    if new.action == "HAND_TO_CUSTOMER":
        result.recipient_source = None
        result.recipient_address_source = None
    if new.action != "UNSPECIFIED" and not (
        new.action == "RESCHEDULE"
        and old.action in {"LEAVE_WITH_NEIGHBOR", "LEAVE_WITH_SECURITY"}
    ):
        result.action = new.action
    result.call_on_arrival = new.call_on_arrival
    return result


def ground_recipient(db, session_id, text, step, details):
    if step.action not in {PlanAction.LEAVE_WITH_NEIGHBOR, PlanAction.LEAVE_WITH_SECURITY}:
        step.named_recipient = step.recipient_address = None
        step.recipient_source = step.recipient_address_source = None
        return
    for field, source_field in (
        ("named_recipient", "recipient_source"),
        ("recipient_address", "recipient_address_source"),
    ):
        evidence = getattr(step, source_field) or getattr(details, source_field)
        known = getattr(details, source_field)
        if evidence is not None and known is not None and evidence.quote == known.quote and evidence.quote not in text:
            evidence = known
        if (
            getattr(step, source_field) is None
            and evidence is not None
            and evidence.turn_id is not None
            and getattr(step, field)
            and getattr(step, field).casefold() != evidence.quote.casefold()
        ):
            raise EvidenceError(source_field, "REQUIRED", "missing", False)
        if evidence is not None:
            value = evidence_text(db, session_id, text, evidence, field=source_field)
            if len(value) > 80:
                raise EvidenceError(source_field, "TOO_LONG", "customer", value in text)
            setattr(step, field, recipient_label(field, value))
            setattr(step, source_field, evidence)
        elif getattr(step, field):
            # Old JSON plans retain compatibility for literal source text.
            sources = db.exec(
                select(Turn.text).where(
                    Turn.session_id == session_id, Turn.speaker == Speaker.CUSTOMER
                )
            ).all()
            if not any(
                getattr(step, field).casefold() in source.casefold() for source in [text, *sources]
            ):
                raise EvidenceError(source_field, "REQUIRED", "missing", False)


def normalize_clarification(decision, details, order, slots):
    if decision.action != PlannerAction.ASK_CLARIFICATION or decision.request_details is None:
        return
    supplied = decision.request_details
    if details.action == "LEAVE_WITH_NEIGHBOR":
        if PlanAction.LEAVE_WITH_NEIGHBOR not in _allowed_alternatives(order):
            decision.proposal = PlanProposal(primary=PlanStep(
                action=PlanAction.LEAVE_WITH_NEIGHBOR,
                window_start=details.window_start or order.window_start,
                window_end=details.window_end or order.window_end,
                call_on_arrival=details.call_on_arrival,
                recipient_source=details.recipient_source,
                recipient_address_source=details.recipient_address_source,
            ))
            decision.action = PlannerAction.PROPOSE_PLAN
            decision.message = "{plan_summary} {confirmation_question}"
            decision.missing_fields = []
            decision.awaiting_continuation = False
            return
        missing = (
            "recipient" if details.recipient_source is None else
            "recipient_address" if details.recipient_address_source is None else None
        )
        if missing:
            decision.missing_fields = [missing]
            decision.awaiting_continuation = False
            return
        decision.missing_fields = [
            field for field in decision.missing_fields if field not in {"recipient", "recipient_address"}
        ]
    completing_time = supplied.window_start is not None or "end_time" in decision.missing_fields
    completing_recipient = (
        (supplied.recipient_source is not None or supplied.recipient_address_source is not None)
        and details.action == "LEAVE_WITH_NEIGHBOR"
    )
    if not (completing_time or completing_recipient):
        return
    if "continuation" in decision.missing_fields and "end_time" not in decision.missing_fields:
        return
    if details.window_start is None:
        if not completing_recipient or {"time", "end_time"}.intersection(decision.missing_fields):
            return
        # A named handoff with no new time keeps the order's scheduled window.
        details = details.model_copy(deep=True)
        details.window_start = _utc(order.window_start).astimezone(IST)
        details.window_end = _utc(order.window_end).astimezone(IST)
    if decision.missing_fields and any(
        field not in {"time", "end_time", "continuation"} for field in decision.missing_fields
    ):
        return
    action = (
        PlanAction(details.action) if details.action != "UNSPECIFIED" else PlanAction.RESCHEDULE
    )
    if action == PlanAction.LEAVE_WITH_NEIGHBOR and details.recipient_source is None:
        return
    start = _utc(details.window_start).astimezone(IST)
    end = details.window_end
    if end is None:
        duration = _utc(order.window_end) - _utc(order.window_start)
        end = start + duration
        containing = [
            slot for slot in slots if _utc(slot.start_at) <= _utc(start) < _utc(slot.end_at)
        ]
        if containing:
            end = min(end, min(_utc(slot.end_at).astimezone(IST) for slot in containing))
        end = min(end, start.replace(hour=18, minute=0, second=0, microsecond=0))
    if _utc(end) <= _utc(start):
        # Retain an unavailable request for the normal policy alternatives path.
        end = start + timedelta(hours=1)
    decision.proposal = PlanProposal(
        primary=PlanStep(
            action=action,
            window_start=start,
            window_end=end,
            call_on_arrival=details.call_on_arrival,
            recipient_source=details.recipient_source,
            recipient_address_source=details.recipient_address_source,
        )
    )
    decision.action = PlannerAction.PROPOSE_PLAN
    decision.message = "{plan_summary} {confirmation_question}"
    decision.awaiting_continuation = False
    decision.missing_fields = []


def save_details(db, session_id, turn_id, details):
    stored = details.model_copy(deep=True)
    for field in ("time_source", "recipient_source", "recipient_address_source"):
        evidence = getattr(stored, field)
        if evidence is not None and evidence.turn_id is None:
            evidence.turn_id = turn_id
    call = db.get(CallSession, session_id)
    record = (
        db.get(PlanProposalRecord, call.current_proposal_id)
        if call and call.current_proposal_id
        else None
    )
    if record:
        proposal = PlanProposal.model_validate_json(record.proposal_json)
        for step in (proposal.primary, proposal.fallback):
            if step is None:
                continue
            for field in ("recipient_source", "recipient_address_source"):
                evidence = getattr(step, field)
                if evidence is not None and evidence.turn_id is None:
                    evidence.turn_id = turn_id
        record.proposal_json = proposal.model_dump_json()
        db.add(record)
    db.add(
        AuditEvent(
            session_id=session_id,
            event_type="REQUEST_DETAILS",
            payload_json=stored.model_dump_json(),
            created_at=utc_now(),
        )
    )
    db.commit()


def clarification_fallback(fields, language):
    kind = next(iter(fields), "other")
    messages = {
        "recipient": (
            "Who should receive the parcel? Please give their name.",
            "पार्सल किसे देना है? उनका नाम बताइए।",
            "Parcel kise dena hai? Unka naam bataiye.",
        ),
        "recipient_address": (
            "Where should the courier find your neighbour? Please give their house or flat number, or clear directions.",
            "कूरियर आपके पड़ोसी को कहाँ मिलेगा? उनका मकान या फ्लैट नंबर, या स्पष्ट रास्ता बताइए।",
            "Courier aapke padosi ko kahan milega? Unka house ya flat number, ya saaf directions bataiye.",
        ),
        "time": (
            "Which delivery time works for you?",
            "डिलीवरी का कौन सा समय आपके लिए ठीक रहेगा?",
            "Delivery ka kaunsa time aapke liye theek rahega?",
        ),
        "other": (
            "I received your words. Could you say a little more about that?",
            "आपकी बात मिली है। इसके बारे में थोड़ा और बताइए।",
            "Aapki baat mili hai. Iske baare mein thoda aur bataiye.",
        ),
    }
    return messages.get(kind, messages["other"])[{"en-IN": 0, "hi-IN": 1, "hi-en": 2}[language]]
