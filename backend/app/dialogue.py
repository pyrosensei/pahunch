"""Source-backed request details and recoverable clarification context."""

import json
from datetime import date, datetime, timedelta

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
            "date_source": "TIME_SOURCE",
            "end_time_source": "TIME_SOURCE",
            "availability_source": "AVAILABILITY_SOURCE",
            "recipient_source": "RECIPIENT_SOURCE",
            "recipient_address_source": "RECIPIENT_ADDRESS_SOURCE",
        }[field]
        super().__init__(f"{prefix}_{reason}")
        self.field = field
        self.reference_kind = reference_kind
        self.current_match = current_match


def delivery_evidence_text(turn: Turn) -> str:
    """Legacy turns remain readable. Classified background cannot ground a plan."""
    payload = json.loads(turn.structured_payload_json)
    if payload.get("relevance") in {"OFF_TOPIC", "AMBIGUOUS", "CALL_CONTROL"}:
        return ""
    if payload.get("relevance") == "MIXED":
        return "\n".join(payload.get("delivery_quotes", []))
    return turn.text


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
    elif evidence.turn_id is None:
        # Providers sometimes repeat an earlier verbatim customer quote while
        # omitting its turn id. Resolve only eligible, same-session evidence.
        recent = db.exec(select(Turn).where(
            Turn.session_id == session_id, Turn.speaker == Speaker.CUSTOMER,
        ).order_by(Turn.id.desc()).limit(10)).all()
        previous = next((turn for turn in recent if evidence.quote in delivery_evidence_text(turn)), None)
        if previous is not None:
            evidence.turn_id = previous.id
    if not current_match and evidence.turn_id is not None:
        turn = db.get(Turn, evidence.turn_id)
        if turn is None:
            reference_kind = "missing"
        elif turn.session_id != session_id:
            reference_kind = "foreign_session"
        else:
            reference_kind = turn.speaker.value.lower()
        if reference_kind != "customer":
            raise EvidenceError(field, "NOT_CUSTOMER", reference_kind, current_match)
        source = delivery_evidence_text(turn)
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


def load_negotiation(db: Session, session_id: str) -> dict:
    event = db.exec(select(AuditEvent).where(
        AuditEvent.session_id == session_id, AuditEvent.event_type == "NEGOTIATION_CONTEXT",
    ).order_by(AuditEvent.id.desc())).first()
    return json.loads(event.payload_json) if event else {"focus": "AVAILABILITY", "information_topics": []}


def save_negotiation(db, session_id, focus, information_topics, discussed_date=None):
    context = load_negotiation(db, session_id)
    context.update(focus=focus, information_topics=information_topics)
    if discussed_date is not None:
        context["discussed_date"] = discussed_date.isoformat()
    db.add(AuditEvent(
        session_id=session_id, event_type="NEGOTIATION_CONTEXT",
        payload_json=json.dumps(context),
        created_at=utc_now(),
    ))
    db.commit()


def effective_preferences(details: RequestDetails) -> tuple[object, ...]:
    # Compare resolved preferences, not evidence references or time-quote wording.
    return (
        _utc(details.window_start) if details.window_start is not None else None,
        _utc(details.window_end) if details.window_end is not None else None,
        details.action,
        details.call_on_arrival,
        details.recipient_source.quote if details.recipient_source is not None else None,
        details.recipient_address_source.quote if details.recipient_address_source is not None else None,
        details.requested_date,
        tuple(details.unavailable_dates),
        details.scheduled_window_available,
    )


def merge_details(db, session_id, text, old, new, *, rejected=None):
    if new is None:
        return old.model_copy(deep=True)
    result = old.model_copy(deep=True)
    if set(new.unavailable_dates) - set(old.unavailable_dates) and new.date_source is None:
        # "This time" can reject the booked window; it does not identify a
        # different calendar date merely because that date was just offered.
        new.unavailable_dates = [day for day in new.unavailable_dates if day in old.unavailable_dates]
        db.add(AuditEvent(session_id=session_id, event_type="AVAILABILITY_EVIDENCE_IGNORED",
                          payload_json=json.dumps({"reason": "CALENDAR_SOURCE_REQUIRED_FOR_DATE_REFUSAL"}), created_at=utc_now()))
    call = db.get(CallSession, session_id)
    order = db.get(Order, call.order_id)
    # Validate independent evidence before mutating the draft. A bad date must
    # not discard a correctly quoted recipient or clock in the same turn.
    new = new.model_copy(deep=True)
    for field in ("time_source", "end_time_source", "date_source", "availability_source", "recipient_source", "recipient_address_source"):
        evidence = getattr(new, field)
        if evidence is None:
            continue
        try:
            evidence_text(db, session_id, text, evidence, field=field)
        except EvidenceError as error:
            if rejected is None:
                raise
            rejected.append(error)
            setattr(new, field, None)
            if field == "date_source":
                new.requested_date = None
                new.date_reference = "UNSPECIFIED"
                new.unavailable_dates = []
                if new.window_start is not None:
                    new.requested_start_clock = _utc(new.window_start).astimezone(IST).time().replace(tzinfo=None)
                new.window_start = new.window_end = None
            elif field == "time_source":
                new.window_start = new.window_end = None
                new.requested_start_clock = new.requested_end_clock = None
            elif field == "end_time_source":
                new.window_end = None
                new.requested_end_clock = None
            elif field == "availability_source":
                new.unavailable_dates = []
                new.scheduled_window_available = None
    recipient_changed = (
        new.recipient_source is not None
        and old.recipient_source is not None
        and new.recipient_source.quote.casefold() != old.recipient_source.quote.casefold()
    )
    if recipient_changed:
        result.recipient_address_source = None
    for field in ("time_source", "end_time_source", "date_source", "availability_source", "recipient_source", "recipient_address_source"):
        evidence = getattr(new, field)
        if evidence is not None:
            known = getattr(old, field)
            if known is not None and known.quote == evidence.quote and evidence.quote not in text:
                evidence = known
            if field == "recipient_address_source" and recipient_changed and evidence.quote not in text:
                continue
            if field == "recipient_address_source" and not usable_recipient_location(evidence.quote):
                result.recipient_address_source = None
                continue
            setattr(result, field, evidence)
    if new.unavailable_dates or new.scheduled_window_available is not None:
        changed = bool(set(new.unavailable_dates) - set(old.unavailable_dates)) or (
            new.scheduled_window_available is not None and new.scheduled_window_available != old.scheduled_window_available
        )
        if changed and new.availability_source is None:
            # A missing refusal quote cannot erase independently verified time
            # or handoff facts. Keep the previous availability and audit it.
            new.unavailable_dates = [day for day in new.unavailable_dates if day in old.unavailable_dates]
            new.scheduled_window_available = old.scheduled_window_available
            db.add(AuditEvent(
                session_id=session_id, event_type="AVAILABILITY_EVIDENCE_IGNORED",
                payload_json=json.dumps({"reason": "AVAILABILITY_SOURCE_REQUIRED"}), created_at=utc_now(),
            ))
        result.unavailable_dates = sorted(set(old.unavailable_dates + new.unavailable_dates))
        if new.scheduled_window_available is not None:
            result.scheduled_window_available = new.scheduled_window_available
    if new.requested_date is not None and new.requested_date not in new.unavailable_dates:
        if new.date_source is None and new.time_source is None and new.date_reference not in {"SCHEDULED", "DISCUSSED"}:
            raise EvidenceError("date_source", "REQUIRED", "missing", False)
        result.requested_date = new.requested_date
        result.date_reference = new.date_reference
        if new.requested_date not in new.unavailable_dates:
            result.unavailable_dates = [day for day in result.unavailable_dates if day != new.requested_date]
    if new.window_start is not None:
        if new.time_source is None and _utc(new.window_start) != _utc(order.window_start):
            raise ValueError("TIME_SOURCE_REQUIRED")
        if result.window_start != new.window_start:
            result.window_end = None
        result.window_start = new.window_start
        result.time_reference = new.time_reference
        if new.time_source is not None:
            result.requested_start_clock = _utc(new.window_start).astimezone(IST).time().replace(tzinfo=None)
            result.requested_end_clock = None
    if new.window_end is not None:
        if new.time_source is None and (
            _utc(new.window_end) != _utc(order.window_end)
            or (new.window_start is not None and _utc(new.window_start) != _utc(order.window_start))
        ):
            raise ValueError("TIME_SOURCE_REQUIRED")
        result.window_end = new.window_end
        if new.end_time_source is not None:
            result.requested_end_clock = _utc(new.window_end).astimezone(IST).time().replace(tzinfo=None)
    if new.time_source is not None:
        for field in ("requested_start_clock", "requested_end_clock"):
            if getattr(new, field) is not None:
                setattr(result, field, getattr(new, field))
    action = new.handoff_action or new.action
    if action == "HAND_TO_CUSTOMER":
        result.recipient_source = None
        result.recipient_address_source = None
    if action != "UNSPECIFIED" and not (
        action == "RESCHEDULE"
        and old.action in {"LEAVE_WITH_NEIGHBOR", "LEAVE_WITH_SECURITY"}
    ):
        result.action = action
    result.call_on_arrival = new.call_on_arrival
    return result


def normalize_relative_day(db, session_id, text, decision, call_date, *, known=None):
    details = decision.request_details
    if details is None:
        return
    primary = decision.proposal.primary if decision.proposal else None
    known = known or load_details(db, session_id)
    if details.requested_date in details.unavailable_dates and details.window_start is None and primary is None:
        return
    source = details.date_source or details.time_source
    absolute = details.date_reference == "ABSOLUTE" and details.requested_date is not None
    if details.date_reference in {"SCHEDULED", "DISCUSSED"}:
        call = db.get(CallSession, session_id)
        order = db.get(Order, call.order_id)
        if details.date_reference == "SCHEDULED":
            requested_date = _utc(order.window_start).astimezone(IST).date()
        else:
            discussed = load_negotiation(db, session_id).get("discussed_date")
            if discussed is None:
                raise EvidenceError("date_source", "REQUIRED", "missing", False)
            requested_date = date.fromisoformat(discussed)
        # Calendar dates for these references come from validated server state.
        # Provider annotations quoting an agent date are unnecessary.
        details.date_source = None
        details.relative_day_source = None
        details.requested_date = requested_date
    elif absolute:
        if source is None:
            raise EvidenceError("date_source", "REQUIRED", "missing", False)
        evidence_text(db, session_id, text, source, field="date_source")
        requested_date = details.requested_date
        if details.relative_day_source is not None:
            db.add(AuditEvent(session_id=session_id, event_type="DATE_ANNOTATION_IGNORED",
                              payload_json=json.dumps({"reason": "VERIFIED_ABSOLUTE_DATE"}), created_at=utc_now()))
            details.relative_day_source = None
    elif details.date_reference in {"TODAY", "TOMORROW"} or details.relative_day_source is not None:
        relative_source = details.relative_day_source or details.date_source
        if relative_source is None:
            raise EvidenceError("date_source", "REQUIRED", "missing", False)
        quote = evidence_text(db, session_id, text, relative_source, field="date_source")
        offsets = {"today": 0, "aaj": 0, "आज": 0, "tomorrow": 1, "kal": 1, "कल": 1}
        offset = offsets.get(quote.casefold().strip())
        if offset is None:
            raise ValueError("TIME_RELATIVE_DAY_AMBIGUOUS")
        requested_date = call_date + timedelta(days=offset)
        details.requested_date = requested_date
        details.date_source = relative_source
        details.date_reference = "TODAY" if offset == 0 else "TOMORROW"
    elif details.requested_start_clock is not None and known.requested_date is not None:
        requested_date = known.requested_date
    else:
        return
    start = details.window_start or (primary.window_start if primary else None) or known.window_start
    clock = details.requested_start_clock
    if clock is None and details.window_start is None and details.time_source is None:
        clock = known.requested_start_clock
    clock_source = details.time_source or known.time_source
    if clock is not None and clock_source is not None:
        evidence_text(db, session_id, text, clock_source, field="time_source")
        details.time_source = clock_source
        details.time_reference = "START_ONLY" if details.requested_end_clock is None else "EXPLICIT_WINDOW"
        start = datetime.combine(requested_date, clock, IST)
        if details.requested_end_clock is not None:
            details.window_end = datetime.combine(requested_date, details.requested_end_clock, IST)
    if start is None:
        return
    local = _utc(start).astimezone(IST)
    shift = timedelta(days=(requested_date - local.date()).days)
    details.window_start = local + shift
    if details.window_end is not None:
        details.window_end = _utc(details.window_end).astimezone(IST) + shift
    if primary is not None and primary.window_start is not None:
        primary_start = _utc(primary.window_start).astimezone(IST)
        primary_shift = timedelta(days=(requested_date - primary_start.date()).days)
        primary.window_start = primary_start + primary_shift
        primary.window_end = _utc(primary.window_end).astimezone(IST) + primary_shift


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
            turns = db.exec(
                select(Turn).where(
                    Turn.session_id == session_id, Turn.speaker == Speaker.CUSTOMER
                )
            ).all()
            if not any(
                getattr(step, field).casefold() in source.casefold()
                for source in [text, *[delivery_evidence_text(turn) for turn in turns]]
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
    completing_handoff = (supplied.handoff_action or supplied.action) == "HAND_TO_CUSTOMER"
    if not (completing_time or completing_recipient or completing_handoff):
        return
    if "continuation" in decision.missing_fields and "end_time" not in decision.missing_fields:
        return
    if details.window_start is None:
        if not (completing_recipient or completing_handoff) or {"time", "end_time"}.intersection(decision.missing_fields):
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
    for field in ("time_source", "end_time_source", "date_source", "availability_source", "recipient_source", "recipient_address_source"):
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
    if language == "hi-en":
        language = "hi-IN"
    kind = next(iter(fields), "other")
    messages = {
        "date": (
            "Which delivery date should I check? I have kept your requested time.",
            "डिलीवरी की कौन सी तारीख चाहिए? आपका बताया समय याद है।",
            "डिलीवरी की कौन सी तारीख चाहिए? आपका बताया समय याद है।",
        ),
        "recipient": (
            "Who should receive the parcel? Please give their name.",
            "पार्सल किसे देना है? उनका नाम बताइए।",
            "Parcel किसे देना है? उनका नाम बताइए।",
        ),
        "recipient_address": (
            "Where should the courier find your neighbour? Please give their house or flat number, or clear directions.",
            "कूरियर आपके पड़ोसी को कहाँ मिलेगा? उनका मकान या फ्लैट नंबर, या स्पष्ट रास्ता बताइए।",
            "Courier आपके पड़ोसी को कहाँ मिलेगा? उनका house या flat number, या साफ़ directions बताइए।",
        ),
        "time": (
            "Which delivery time works for you?",
            "डिलीवरी का कौन सा समय आपके लिए ठीक रहेगा?",
            "Delivery का कौन सा time आपके लिए ठीक रहेगा?",
        ),
        "other": (
            "Would you like to receive the parcel at the scheduled time, or arrange another time or recipient?",
            "क्या आप तय समय पर पार्सल लेंगे, या समय या पार्सल लेने वाला व्यक्ति बदलना है?",
            "क्या आप scheduled time पर parcel लेंगे, या time या parcel लेने वाला व्यक्ति बदलना है?",
        ),
    }
    return messages.get(kind, messages["other"])[{"en-IN": 0, "hi-IN": 1, "hi-en": 2}[language]]
