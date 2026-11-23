"""Acceptance and speech regressions from the recorded order-1076 call."""

import json
from datetime import datetime

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import run, start_call

from app.consent import is_explicit_confirmation
from app.dialogue import evidence_text, load_details
from app.errors import DomainError
from app.language import conversation_language, phrase
from app.models import AuditEvent, DeliveryCommit, DeliverySlot, Order, PaymentMode, Speaker, Turn
from app.planner import PlannerService, outbound_opening
from app.schemas import CustomerEvidence, CustomerTurnRequest, PlanProposal

db = planner_database
TRANSCRIPT = [
    "इसी date पर कोई और समय हो सकता है?",
    "दोपहर के तीन बजे तक ठीक रहेगा मेरे लिए, और ऊपर से मैं घर पर भी नहीं हूँ। क्या आप किसी और को दे सकते हैं दोपहर के तीन बजे?",
    "आप फिर एक काम कीजिए, मुझे ही दे दीजिए। आप कल दे दीजिए मुझे, seven परसों दे दीजिए, seventeen को। दोपहर के तीन बजे।",
]


def laptop_order(db):
    order = db.get(Order, 1042)
    order.window_start = datetime.fromisoformat("2026-11-16T10:00:00+05:30")
    order.window_end = order.window_start.replace(hour=12)
    order.payment_mode = PaymentMode.PREPAID
    order.requires_otp = True
    order.declared_value_inr = 69999
    for slot in db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order.id)).all():
        db.delete(slot)
    for day in (16, 17):
        start = order.window_start.replace(day=day)
        db.add(DeliverySlot(order_id=order.id, start_at=start, end_at=start.replace(hour=18)))
    db.add(order)
    db.commit()
    return order


def response(action, **fields):
    return dict(
        action=action,
        message="{plan_summary} {confirmation_question}",
        proposal=None,
        escalation_reason=None,
        confidence=0.99,
        **fields,
    )


def proposal(day=17):
    return {
        "primary": {
            "action": "RESCHEDULE",
            "window_start": f"2026-11-{day}T15:00:00+05:30",
            "window_end": f"2026-11-{day}T17:00:00+05:30",
            "call_on_arrival": True,
        }
    }


def replay_decisions():
    forbidden = proposal(16)
    forbidden["primary"].update(action="LEAVE_WITH_NEIGHBOR", named_recipient="किसी और")
    return [
        response(
            "RESPOND",
            information_topics=["AVAILABILITY"],
            availability_scope="DATE",
            request_details={
                "requested_date": "2026-11-16",
                "date_reference": "SCHEDULED",
                "date_source": {"quote": "इसी date"},
            },
        ),
        {**response("PROPOSE_PLAN"), "proposal": forbidden},
        {
            **response("PROPOSE_PLAN"),
            "proposal": proposal(),
            "request_details": {
                "window_start": "2026-11-17T15:00:00+05:30",
                "time_reference": "START_ONLY",
                "time_source": {"quote": "दोपहर के तीन बजे"},
                "date_reference": "ABSOLUTE",
                "requested_date": "2026-11-17",
                "date_source": {"quote": "seventeen"},
                "handoff_action": "HAND_TO_CUSTOMER",
                "action": "RESCHEDULE",
                "call_on_arrival": True,
            },
        },
    ]


@pytest.mark.parametrize(
    "text",
    [
        "हाँ, चलेगा मुझे।",
        "मेरे लिए ठीक है।",
        "haan chalega",
        "mere liye theek hai",
        "आगे बढ़ाइए।",
        "आगे बढ़ाइए।",
        "आगे बढ़ा दीजिए।",
        "aage badha dijiye",
        "go ahead",
        "please proceed",
        "that works for me",
    ],
)
def test_clear_acceptance_and_proceed_commands(text):
    assert is_explicit_confirmation(text)


@pytest.mark.parametrize("previous", ["en-IN", "hi-IN", "hi-en"])
@pytest.mark.parametrize("text", ["हाँ, चलेगा मुझे।", "आगे बढ़ाइए।", "that works for me"])
def test_acceptance_keeps_established_response_language(previous, text):
    assert conversation_language(text, previous) == previous


@pytest.mark.parametrize(
    "text",
    [
        "हाँ, लेकिन time बदल दीजिए",
        "मत बढ़ाइए",
        "आगे नहीं बढ़ाइए",
        "शायद चलेगा मुझे",
        "पापा को हाँ बोल दो",
        "पापा आगे बढ़ाइए",
        "He said go ahead",
        "that works if you change it",
        "please confirm the time",
        "do I proceed",
        "do I confirm the date",
        "is the delivery time okay",
        "please",
        "चलेगा?",
        "आगे बढ़ाइए लेकिन कल",
        "badhaiye",
        "हाँ, घर 403 में दे देना",
    ],
)
def test_qualified_or_misdirected_agreement_is_rejected(text):
    assert not is_explicit_confirmation(text)


@pytest.mark.parametrize("acceptance", ["हाँ, चलेगा मुझे।", "आगे बढ़ाइए।"])
def test_laptop_conversation_accepts_first_confirmation_and_commits_once(
    db, scripted_llm, acceptance
):
    laptop_order(db)
    llm = scripted_llm(replay_decisions())
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    replies = [run(planner, call.id, text) for text in TRANSCRIPT]
    assert "16 नवंबर" in replies[0].agent_message
    assert any(v.code == "HIGH_VALUE_DIRECT_HANDOFF" for v in replies[1].turn.verdict.violations)
    assert replies[2].turn.verdict.valid and not replies[2].turn.customer_confirmed
    assert replies[2].planner.response_language == "hi-en"
    assert "17 नवंबर" in replies[2].agent_message and "delivery रख सकते हैं" in replies[2].agent_message
    assert "Courier पहुँचने पर आपको call करेगा" in replies[2].agent_message
    assert "बदले हुए" not in replies[2].agent_message
    proposal_id = call.current_proposal_id
    confirmed = run(planner, call.id, acceptance)
    assert confirmed.turn.customer_confirmed
    assert confirmed.agent_message == phrase("confirmation_accepted", "hi-en")
    assert "17 नवंबर" not in confirmed.agent_message and "?" not in confirmed.agent_message
    assert len(llm.requests) == 3
    committed = planner.calls.commit_plan(call.id, "laptop-acceptance")
    assert committed.courier_card.window_start.isoformat().startswith("2026-11-17T15:00")
    assert committed.courier_card.window_end.isoformat().startswith("2026-11-17T17:00")
    assert planner.calls.commit_plan(call.id, "laptop-acceptance").commit_id == committed.commit_id
    assert len(db.exec(select(DeliveryCommit)).all()) == 1
    event = db.exec(
        select(AuditEvent).where(
            AuditEvent.session_id == call.id, AuditEvent.event_type == "CUSTOMER_CONFIRMED"
        )
    ).one()
    assert json.loads(event.payload_json) == {
        "reason": "EXPLICIT_AGREEMENT",
        "proposal_id": proposal_id,
    }


def test_unclear_confirmation_asks_briefly_and_then_accepts_proceed(db, scripted_llm):
    laptop_order(db)
    planner = PlannerService(
        db,
        scripted_llm(
            [
                {**response("PROPOSE_PLAN"), "proposal": proposal()},
                response("CONFIRM_PLAN"),
            ]
        ),
    )
    call = start_call(planner.calls)
    run(planner, call.id, "17 November at 3 PM")
    result = run(planner, call.id, "हम्म")
    assert not result.turn.customer_confirmed
    assert "17 November" not in result.agent_message
    assert "17 नवंबर" not in result.agent_message
    assert len(result.agent_message) < 100
    assert run(planner, call.id, "आगे बढ़ाइए").turn.customer_confirmed


@pytest.mark.parametrize("acceptance", ["हाँ, चलेगा मुझे।", "आगे बढ़ाइए।"])
def test_shared_customer_api_confirmation_and_low_confidence_gate(db, acceptance):
    laptop_order(db)
    service = PlannerService(db, None).calls
    call = start_call(service)
    service.submit_turn(
        call.id,
        CustomerTurnRequest(text="Use this time", proposal=PlanProposal.model_validate(proposal())),
    )
    with pytest.raises(DomainError, match="Explicit customer agreement"):
        service.submit_turn(
            call.id,
            CustomerTurnRequest(text=acceptance, confirmation=True, transcript_confidence=0.1),
        )
    assert not call.customer_confirmed
    assert service.submit_turn(
        call.id, CustomerTurnRequest(text=acceptance, confirmation=True)
    ).customer_confirmed
    assert service.commit_plan(call.id, "direct-api").state == "COMMITTED"


def test_hinglish_opening_and_prompt_offer_conversational_wrappers(db, scripted_llm):
    laptop_order(db)
    llm = scripted_llm([{**response("PROPOSE_PLAN"), "proposal": proposal()}])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    opening, meta = outbound_opening(db, call.id)
    assert meta.response_language == "hi-en"
    assert "delivery" in opening and "मैं" in opening and "OTP" in opening
    run(planner, call.id, "कल तीन बजे कर दीजिए")
    prompt = llm.requests[0]
    assert "natural Roman Hinglish" not in prompt["system_prompt"]
    context = json.loads(prompt["user_prompt"])
    assert context["response_language"] == "hi-en"
    assert all("बदले हुए" not in wrapper for wrapper in context["proposal_wording_templates"])


@pytest.mark.parametrize("prefix", ["बदले हुए निर्देश के लिए:", "For the revised instruction:"])
def test_administrative_wrapper_gets_conversational_fallback(db, scripted_llm, prefix):
    laptop_order(db)
    item = {
        **response("PROPOSE_PLAN"),
        "message": prefix + " {plan_summary} {confirmation_question}",
        "proposal": proposal(),
    }
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "17 November at 3 PM")
    assert result.turn.verdict.valid
    assert result.planner.wording_source == "template" and result.planner.fallback_reason
    assert prefix not in result.agent_message
    assert (
        "17 November" in result.agent_message and "We can arrange delivery" in result.agent_message
    )


@pytest.mark.parametrize(
    "relevance,allowed", [("DELIVERY", True), ("OFF_TOPIC", False), ("CALL_CONTROL", False)]
)
def test_missing_evidence_id_can_only_rebind_eligible_customer_quote(db, relevance, allowed):
    planner = PlannerService(db, None)
    call = start_call(planner.calls)
    old = Turn(
        session_id=call.id,
        speaker=Speaker.CUSTOMER,
        text="इसी date पर कोई और समय?",
        structured_payload_json=json.dumps({"relevance": relevance}),
        created_at=call.started_at,
    )
    db.add(old)
    db.commit()
    evidence = CustomerEvidence(quote="इसी date")
    if allowed:
        assert (
            evidence_text(db, call.id, "दोपहर तीन बजे", evidence, field="date_source") == "इसी date"
        )
        assert evidence.turn_id == old.id
    else:
        with pytest.raises(ValueError, match="MISMATCH"):
            evidence_text(db, call.id, "दोपहर तीन बजे", evidence, field="date_source")


def test_unverified_availability_does_not_discard_valid_time_and_policy_rejection(db, scripted_llm):
    laptop_order(db)
    decisions = replay_decisions()
    decisions[1]["request_details"] = {
        "scheduled_window_available": False,
        "window_start": "2026-11-16T15:00:00+05:30",
        "time_reference": "START_ONLY",
        "time_source": {"quote": "दोपहर के तीन बजे"},
        "handoff_action": "LEAVE_WITH_NEIGHBOR",
    }
    planner = PlannerService(db, scripted_llm(decisions))
    call = start_call(planner.calls)
    run(planner, call.id, TRANSCRIPT[0])
    result = run(planner, call.id, TRANSCRIPT[1])
    assert result.planner.error_code is None
    assert any(v.code == "HIGH_VALUE_DIRECT_HANDOFF" for v in result.turn.verdict.violations)
    assert load_details(db, call.id).scheduled_window_available is None
    assert load_details(db, call.id).window_start.hour == 15
