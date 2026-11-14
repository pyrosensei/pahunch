"""Audience/relevance regressions. Real-model replays separately verify classification."""

import json

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import run, start_call, valid_proposal

from app.calls import MAX_TURNS, utc_now
from app.dialogue import EvidenceError, evidence_text, load_details, save_details
from app.errors import DomainError
from app.models import (
    CallState,
    DeliveryCommit,
    Order,
    OrderStatus,
    PlanProposalRecord,
    Speaker,
    Turn,
)
from app.planner import PlannerService
from app.schemas import (
    CustomerEvidence,
    CustomerTurnRequest,
    PlanProposal,
    RequestDetails,
    TurnRelevance,
)

db = planner_database

# Eight categories in three language styles, replayed at four delivery stages.
RELEVANCE_CASES = [
    (
        "household",
        "OFF_TOPIC",
        ["Son, how many rotis will you eat?", "बेटा कितनी रोटी लेगा?", "Beta kitni roti lega?"],
    ),
    (
        "numbers",
        "OFF_TOPIC",
        [
            "Put five cups and six plates on the table.",
            "पाँच कप और छह प्लेट मेज पर रख दो।",
            "Paanch cup aur chhe plate table par rakho.",
        ],
    ),
    (
        "unrelated_recipient",
        "OFF_TOPIC",
        [
            "Give the milk to Sharma at house 403.",
            "दूध शर्मा जी को मकान 403 में दे देना।",
            "Doodh Sharma ji ko house 403 mein de dena.",
        ],
    ),
    (
        "general_question",
        "OFF_TOPIC",
        ["Who won the cricket match?", "क्रिकेट मैच कौन जीता?", "Cricket match kaun jeeta?"],
    ),
    (
        "quoted_agreement",
        "OFF_TOPIC",
        [
            "Mum said yes to dinner, she was talking to my brother.",
            "मम्मी ने खाने के लिए हाँ कहा, भाई से बात कर रही थीं।",
            "Mummy ne dinner ke liye haan bola, bhai se baat thi.",
        ],
    ),
    (
        "unclear",
        "AMBIGUOUS",
        [
            "That one over there, you know?",
            "वो जो वहाँ है, वही वाली बात।",
            "Woh jo wahan hai, wahi wali baat.",
        ],
    ),
    (
        "change_subject",
        "OFF_TOPIC",
        [
            "Can you teach me algebra instead?",
            "मुझे गणित सिखा सकती हो?",
            "Mujhe algebra sikha sakti ho?",
        ],
    ),
    (
        "mixed",
        "MIXED",
        [
            "Mum, make tea. Hand the parcel directly to me at the scheduled time.",
            "मम्मी चाय बना दो। पार्सल तय समय पर मुझे ही देना।",
            "Mummy chai bana do. Parcel tay samay par mujhe hi dena.",
        ],
    ),
]
STAGES = ["opening", "time", "recipient_address", "confirmation"]


def decision(relevance="OFF_TOPIC", message="{delivery_purpose} {pending_question}", **changes):
    value = {
        "action": "RESPOND",
        "relevance": relevance,
        "delivery_quotes": [],
        "message": message,
        "proposal": None,
        "escalation_reason": None,
        "confidence": 0.95,
    }
    value.update(changes)
    return value


def agent_turn(db, call, *, action="ASK_CLARIFICATION", missing_fields=None):
    db.add(
        Turn(
            session_id=call.id,
            speaker=Speaker.AGENT,
            text="Pending delivery question",
            created_at=utc_now(),
            structured_payload_json=json.dumps(
                {
                    "planner": {
                        "action": action,
                        "missing_fields": missing_fields or [],
                        "response_language": "hi-IN",
                    }
                }
            ),
        )
    )
    db.commit()


def prepare_stage(db, planner, stage):
    call = start_call(planner.calls)
    if stage == "confirmation":
        planner.calls.submit_turn(
            call.id,
            CustomerTurnRequest(
                text="Hand it directly to me at the scheduled time",
                proposal=PlanProposal.model_validate(valid_proposal()),
            ),
        )
        agent_turn(db, call, action="PROPOSE_PLAN")
    elif stage in {"time", "recipient_address"}:
        agent_turn(db, call, missing_fields=[stage])
    return call


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("language_index", range(3))
@pytest.mark.parametrize(
    "category,relevance,texts", RELEVANCE_CASES, ids=[case[0] for case in RELEVANCE_CASES]
)
def test_relevance_matrix(db, scripted_llm, stage, language_index, category, relevance, texts):
    text = texts[language_index]
    output = decision(relevance)
    if relevance == "MIXED":
        output.update(
            action="PROPOSE_PLAN",
            message="{plan_summary} {confirmation_question}",
            proposal=valid_proposal(),
            delivery_quotes=[text.split(". ")[-1].split("। ")[-1]],
        )
    planner = PlannerService(db, scripted_llm([output]))
    call = prepare_stage(db, planner, stage)
    before = (
        call.state,
        call.current_proposal_id,
        call.customer_confirmed,
        call.clarification_count,
    )
    details = load_details(db, call.id)
    result = run(planner, call.id, text)
    assert result.planner.relevance.value == relevance
    assert not result.turn.customer_confirmed
    assert not list(db.exec(select(DeliveryCommit)).all())
    saved = db.get(Turn, result.turn.turn_id)
    assert saved.text == text
    assert json.loads(saved.structured_payload_json)["relevance"] == relevance
    assert "आपकी बात मिली" not in result.agent_message
    assert "baat sun li" not in result.agent_message
    if relevance != "MIXED":
        assert (
            call.state,
            call.current_proposal_id,
            call.customer_confirmed,
            call.clarification_count,
        ) == before
        assert load_details(db, call.id) == details
        if stage == "confirmation":
            assert result.planner.presented_proposal_id == call.current_proposal_id
            assert result.planner.action.value == "PROPOSE_PLAN"
        else:
            assert result.planner.missing_fields == (
                [stage] if stage != "opening" else ["availability"]
            )
    else:
        assert result.turn.verdict.valid
    context = json.loads(planner.llm.requests[0]["user_prompt"])
    assert context["pending_delivery_question"]


@pytest.mark.parametrize("state", [CallState.COMMITTED, CallState.CANCELLED, CallState.ESCALATED])
@pytest.mark.parametrize("language_index", range(3))
@pytest.mark.parametrize("variant", [0, 1])
def test_terminal_background_cannot_reopen_or_mutate(
    db, scripted_llm, state, language_index, variant
):
    planner = PlannerService(db, scripted_llm([]))
    call = start_call(planner.calls)
    call.state = state
    db.add(call)
    db.commit()
    text = RELEVANCE_CASES[variant][2][language_index]
    with pytest.raises(DomainError, match="not available"):
        run(planner, call.id, text)
    assert call.state == state and call.turn_count == 0
    assert planner.llm.requests == []


@pytest.mark.parametrize("relevance", ["OFF_TOPIC", "AMBIGUOUS", "CALL_CONTROL"])
def test_classified_non_delivery_action_cannot_save_a_plan(db, scripted_llm, relevance):
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(
                    relevance,
                    action="PROPOSE_PLAN",
                    proposal=valid_proposal(),
                )
            ]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "Mummy, put dinner at five, please.")
    assert result.planner.fallback_reason == "NON_DELIVERY_ACTION"
    assert not list(db.exec(select(PlanProposalRecord)).all())


def test_redirect_then_explicit_agreement_confirms_existing_proposal(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([decision()]))
    call = prepare_stage(db, planner, "confirmation")
    proposal_id = call.current_proposal_id
    result = run(planner, call.id, "Mummy said yes to dinner.")
    assert not result.turn.customer_confirmed
    assert result.planner.presented_proposal_id == proposal_id
    confirmed = run(planner, call.id, "Yes, I confirm this plan")
    assert confirmed.turn.customer_confirmed and call.current_proposal_id == proposal_id
    committed = planner.calls.commit_plan(call.id, "relevance-confirm")
    assert planner.calls.commit_plan(call.id, "relevance-confirm").commit_id == committed.commit_id


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_mixed_instruction_misread_as_agreement_gets_one_bounded_repair(
    db, scripted_llm, repair_succeeds
):
    text = "Mum, make tea. Hand the parcel directly to me at the scheduled time."
    quotes = ["Hand the parcel directly to me at the scheduled time."]
    mistaken = decision("MIXED", action="CONFIRM_PLAN", delivery_quotes=quotes)
    corrected = decision(
        "MIXED",
        action="PROPOSE_PLAN",
        proposal=valid_proposal(),
        message="{plan_summary} {confirmation_question}",
        delivery_quotes=quotes,
    )
    planner = PlannerService(
        db, scripted_llm([mistaken, corrected if repair_succeeds else mistaken])
    )
    call = prepare_stage(db, planner, "confirmation")
    result = run(planner, call.id, text)
    assert len(planner.llm.requests) == 2
    assert not result.turn.customer_confirmed
    if repair_succeeds:
        assert result.turn.verdict.valid
        assert result.planner.action.value == "PROPOSE_PLAN"
    else:
        assert result.planner.error_code == "LLM_INVALID_OUTPUT"
    assert any(event.event_type == "LLM_REPAIR" for event in planner.calls.list_events(call.id))


@pytest.mark.parametrize(
    "relevance", [TurnRelevance.OFF_TOPIC, TurnRelevance.AMBIGUOUS, TurnRelevance.CALL_CONTROL]
)
def test_background_between_consent_and_commit_preserves_consent(db, scripted_llm, relevance):
    planner = PlannerService(db, scripted_llm([decision(relevance.value)]))
    call = prepare_stage(db, planner, "confirmation")
    run(planner, call.id, "Yes, I confirm this plan")
    proposal_id = call.current_proposal_id
    run(planner, call.id, "Mum, please make tea")
    assert call.customer_confirmed and call.current_proposal_id == proposal_id
    assert planner.calls.commit_plan(call.id, "pending-commit").commit_id


def test_off_topic_evidence_is_unavailable_on_follow_up(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([decision()]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "Give the milk to Sharma at house 403")
    with pytest.raises(EvidenceError, match="MISMATCH"):
        evidence_text(
            db, call.id, "To him", CustomerEvidence(turn_id=result.turn.turn_id, quote="Sharma")
        )
    assert planner._recent_turns(call.id)[0]["delivery_evidence"] == ""


def test_mixed_evidence_excludes_background_name_and_address(db, scripted_llm):
    text = "Give milk to Sharma at house 403. Deliver my parcel at the scheduled time to me."
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(
                    "MIXED",
                    action="PROPOSE_PLAN",
                    proposal=valid_proposal(),
                    message="{plan_summary} {confirmation_question}",
                    delivery_quotes=["Deliver my parcel at the scheduled time to me."],
                )
            ]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    with pytest.raises(EvidenceError, match="MISMATCH"):
        evidence_text(
            db, call.id, "To him", CustomerEvidence(turn_id=result.turn.turn_id, quote="Sharma")
        )
    assert (
        evidence_text(
            db,
            call.id,
            "Again",
            CustomerEvidence(
                turn_id=result.turn.turn_id,
                quote="scheduled time",
            ),
        )
        == "scheduled time"
    )


@pytest.mark.parametrize("quotes", [[], ["Invented delivery statement"]])
def test_missing_or_invented_mixed_spans_only_clarify(db, scripted_llm, quotes):
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(
                    "MIXED",
                    action="PROPOSE_PLAN",
                    proposal=valid_proposal(),
                    delivery_quotes=quotes,
                )
            ]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "Mummy, tea. Parcel to me.")
    assert result.planner.relevance == TurnRelevance.AMBIGUOUS
    assert not list(db.exec(select(PlanProposalRecord)).all())


@pytest.mark.parametrize("language_index", range(3))
@pytest.mark.parametrize("topic", ["audio", "pause", "repeat"])
def test_call_control_retains_meaning(db, scripted_llm, language_index, topic):
    controls = {
        "audio": [
            ("I cannot hear your voice", "I can repeat that. Could you hear this reply?"),
            ("आपकी आवाज नहीं आ रही", "मैं दोहरा सकती हूँ। क्या अब मेरी आवाज आ रही है?"),
            ("Aapki aawaz nahi aa rahi", "Main dobara keh sakti hoon. Ab meri aawaz aa rahi hai?"),
        ],
        "pause": [
            ("Hold on, one minute", "Take your time."),
            ("एक मिनट रुकिए", "जी, आराम से।"),
            ("Ek minute rukiye", "Ji, aaram se."),
        ],
        "repeat": [
            ("Please repeat the question", "{pending_question}"),
            ("सवाल दोहरा दीजिए", "{pending_question}"),
            ("Sawaal dobara boliye", "{pending_question}"),
        ],
    }
    text, message = controls[topic][language_index]
    planner = PlannerService(db, scripted_llm([decision("CALL_CONTROL", message=message)]))
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert result.planner.relevance == TurnRelevance.CALL_CONTROL
    assert result.planner.wording_source == "model"
    assert not result.turn.customer_confirmed
    assert "cannot hear you" not in result.agent_message
    if topic != "repeat":
        assert result.agent_message == message


def test_unsafe_audio_response_retains_communication_topic(db, scripted_llm):
    planner = PlannerService(
        db, scripted_llm([decision("CALL_CONTROL", message="Your OTP is 123456")])
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "Your audio is breaking up")
    assert "audio" in result.agent_message.lower()
    assert "cannot hear you" not in result.agent_message
    assert "123456" not in result.agent_message


def test_explicit_pause_during_confirmation_waits_without_another_question(db, scripted_llm):
    planner = PlannerService(
        db, scripted_llm([decision("CALL_CONTROL", message="Take your time.")])
    )
    call = prepare_stage(db, planner, "confirmation")
    proposal_id = call.current_proposal_id
    result = run(planner, call.id, "Please hold on, one minute")
    assert result.agent_message == "Take your time."
    assert result.planner.presented_proposal_id is None
    assert call.current_proposal_id == proposal_id and not call.customer_confirmed


@pytest.mark.parametrize(
    "text", ["Mum, cancel it", "मम्मी, टीवी रद्द कर दीजिए", "Mummy TV cancel kar do"]
)
def test_background_cancellation_cannot_cancel_this_order(db, scripted_llm, text):
    planner = PlannerService(db, scripted_llm([decision()]))
    call = start_call(planner.calls)
    order = db.get(Order, call.order_id)
    order.status = OrderStatus.PENDING
    db.add(order)
    db.commit()
    result = run(planner, call.id, text)
    assert result.planner.relevance == TurnRelevance.OFF_TOPIC
    assert order.status == OrderStatus.PENDING
    assert call.state == CallState.DISCOVERY


def test_contextual_delivery_cancellation_still_uses_shipment_policy(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([decision("DELIVERY")]))
    call = start_call(planner.calls)
    order = db.get(Order, call.order_id)
    order.status = OrderStatus.PENDING
    db.add(order)
    db.commit()
    result = run(planner, call.id, "कोई नहीं मेरा ऑर्डर कैंसल कर दीजिए")
    assert result.state == "CANCELLED" and order.status == OrderStatus.CANCELLED


def test_repeated_background_respects_overall_turn_budget(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([decision()] * (MAX_TURNS + 1)))
    call = start_call(planner.calls)
    for _ in range(MAX_TURNS + 1):
        result = run(planner, call.id, "How many rotis?")
    assert result.state == "TURN_BUDGET_EXHAUSTED"
    assert call.clarification_count == 0


def test_background_then_house_number_retains_neighbour(db, scripted_llm):
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(),
                decision(
                    "DELIVERY",
                    action="ASK_CLARIFICATION",
                    message="Which delivery time works for you?",
                    missing_fields=["time"],
                    request_details={"recipient_address_source": {"quote": "house 404"}},
                ),
            ]
        ),
    )
    call = start_call(planner.calls)
    first = planner.calls.submit_turn(call.id, CustomerTurnRequest(text="My neighbour Aviral"))
    save_details(
        db,
        call.id,
        first.turn_id,
        RequestDetails(
            action="LEAVE_WITH_NEIGHBOR",
            recipient_source=CustomerEvidence(quote="Aviral"),
        ),
    )
    agent_turn(db, call, missing_fields=["recipient_address"])
    run(planner, call.id, "Son, do your homework")
    run(planner, call.id, "house 404")
    details = load_details(db, call.id)
    assert details.recipient_source.quote == "Aviral"
    assert details.recipient_address_source.quote == "house 404"


@pytest.mark.parametrize("language_index", range(3))
@pytest.mark.parametrize(
    "scenario", ["recovery", "repeated", "quoted_yes", "correction", "audio", "pause"]
)
def test_multiturn_relevance_recovery(db, scripted_llm, language_index, scenario):
    confirmations = [
        "Yes, I confirm this plan",
        "हाँ, इस प्लान को आगे बढ़ाइए",
        "Haan, is plan ko aage badhaiye",
    ]
    delivery = [
        "Hand the parcel to me at the scheduled time",
        "पार्सल तय समय पर मुझे देना",
        "Parcel tay samay par mujhe dena",
    ][language_index]
    background = RELEVANCE_CASES[0][2][language_index]
    replies = [decision()]
    utterances = [background]
    if scenario == "repeated":
        replies *= 3
        utterances *= 3
    elif scenario == "quoted_yes":
        utterances = [RELEVANCE_CASES[4][2][language_index]]
    elif scenario == "audio":
        utterances = [
            "Your voice is breaking up",
            "आपकी आवाज टूट रही है",
            "Aapki aawaz toot rahi hai",
        ][language_index : language_index + 1]
        replies = [
            decision(
                "CALL_CONTROL",
                message=[
                    "I can repeat that. Could you hear this reply?",
                    "मैं दोहरा सकती हूँ। क्या अब मेरी आवाज आ रही है?",
                    "Main dobara keh sakti hoon. Ab meri aawaz aa rahi hai?",
                ][language_index],
            )
        ]
    elif scenario == "pause":
        utterances = [["Hold on a moment", "एक मिनट रुकिए", "Ek minute rukiye"][language_index]]
        replies = [
            decision(
                "CALL_CONTROL",
                message=["Take your time.", "जी, आराम से।", "Ji, aaram se."][language_index],
            )
        ]
    replies.append(
        decision(
            "DELIVERY",
            action="PROPOSE_PLAN",
            proposal=valid_proposal(),
            message="{plan_summary} {confirmation_question}",
        )
    )
    utterances.append(delivery)
    if scenario == "correction":
        old_name, new_name = [("Aviral", "Sharma"), ("अविरल", "शर्मा जी"), ("Aviral", "Sharma ji")][
            language_index
        ]
        old_text = f"Give my parcel to {old_name}, house 403"
        new_text = f"Sorry, not {old_name}, give my parcel to {new_name}, house 404"

        def neighbour_reply(name, location):
            proposal = valid_proposal()
            proposal["fallback"] = None
            proposal["primary"].update(
                action="LEAVE_WITH_NEIGHBOR",
                named_recipient=name,
                recipient_address=location,
                recipient_source={"quote": name},
                recipient_address_source={"quote": location},
            )
            return decision(
                "DELIVERY",
                action="PROPOSE_PLAN",
                proposal=proposal,
                message="{plan_summary} {confirmation_question}",
                request_details={
                    "action": "LEAVE_WITH_NEIGHBOR",
                    "recipient_source": {"quote": name},
                    "recipient_address_source": {"quote": location},
                },
            )

        replies = [
            neighbour_reply(old_name, "house 403"),
            decision(),
            neighbour_reply(new_name, "house 404"),
        ]
        utterances = [old_text, background, new_text]
    planner = PlannerService(db, scripted_llm(replies))
    call = start_call(planner.calls)
    for utterance in utterances:
        result = run(planner, call.id, utterance)
        assert not result.turn.customer_confirmed
    assert result.turn.verdict.valid
    confirmed = run(planner, call.id, confirmations[language_index])
    assert confirmed.turn.customer_confirmed
    card = planner.calls.commit_plan(call.id, f"multi-{scenario}").courier_card
    if scenario == "correction":
        assert card.named_recipient == new_name
        assert "404" in card.recipient_address
        assert old_name not in " ".join(card.instructions)
        assert "403" not in " ".join(card.instructions)


@pytest.mark.parametrize("language_index", range(3))
def test_repaired_typed_handoff_overrides_mistaken_confirmation_action(
    db, scripted_llm, language_index
):
    text = RELEVANCE_CASES[-1][2][language_index]
    quote = text.split(". ")[-1].split("। ")[-1]
    mistaken = decision("MIXED", action="CONFIRM_PLAN", delivery_quotes=[quote])
    extracted = decision(
        "MIXED",
        action="CONFIRM_PLAN",
        delivery_quotes=[quote],
        request_details={"action": "HAND_TO_CUSTOMER", "handoff_action": "HAND_TO_CUSTOMER"},
    )
    planner = PlannerService(db, scripted_llm([mistaken, extracted]))
    call = prepare_stage(db, planner, "confirmation")
    result = run(planner, call.id, text)
    assert len(planner.llm.requests) == 2
    assert result.planner.action.value == "PROPOSE_PLAN"
    assert result.planner.relevance == TurnRelevance.MIXED
    assert result.turn.verdict.valid and not result.turn.customer_confirmed
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "MIXED_INSTRUCTION_REPAIRED"
