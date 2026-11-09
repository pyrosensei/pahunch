import json
from datetime import datetime

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import run, start_call, valid_proposal

from app.calls import MAX_CLARIFICATIONS, MAX_TURNS
from app.language import conversation_language, mentioned_date, mentioned_time, phrase
from app.llm import LLMTimeoutError
from app.models import Order, PaymentMode, PlanProposalRecord, Turn
from app.planner import PlannerService
from app.schemas import PlannerDecision

db = planner_database


def reply(action, message, proposal=None, confidence=0.95):
    return {
        "action": action,
        "message": message,
        "proposal": proposal,
        "escalation_reason": None,
        "confidence": confidence,
    }


@pytest.mark.parametrize(
    "text,key,language",
    [
        ("Hello", "greeting", "en-IN"),
        ("नमस्ते", "greeting", "hi-IN"),
        ("Namaste", "greeting", "hi-IN"),
        ("Can you hear me?", "heard", "en-IN"),
        ("आपको मेरी आवाज़ आ रही?", "heard", "hi-IN"),
        ("आपको मेरी आवाज़ आ रही है?", "heard", "hi-IN"),
    ],
)
def test_unsafe_conversational_reply_uses_matching_contextual_fallback(
    db, scripted_llm, text, key, language
):
    planner = PlannerService(db, scripted_llm([reply("RESPOND", "I will deliver the parcel.")]))
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert result.agent_message == phrase(key, language)
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert result.turn.verdict is None
    rejected = [
        json.loads(event.payload_json)
        for event in planner.calls.list_events(call.id)
        if event.event_type == "WORDING_REJECTED"
    ]
    assert rejected[-1] == {
        "action": "RESPOND",
        "reason": "UNSAFE_WORDING",
        "wording": "I will deliver the parcel.",
        "outcome": "replaced",
        "fallback": key,
    }


@pytest.mark.parametrize(
    "text,previous,expected",
    [
        ("Hello", "hi-IN", "en-IN"),
        ("Hello", "en-IN", "en-IN"),
        ("yes", "hi-IN", "hi-IN"),
        ("I would like to receive the parcel tomorrow ji", "en-IN", "en-IN"),
        ("I can receive the parcel tomorrow जी", "en-IN", "en-IN"),
        ("मैं available नहीं हूं please deliver tomorrow evening", "hi-IN", "hi-en"),
        ("Haan mujhe kal parcel de dena", "en-IN", "hi-en"),
        ("हाँ", "en-IN", "en-IN"),
        ("okay", "en-IN", "en-IN"),
        ("ठीक है", "en-IN", "en-IN"),
        ("Cancel delivery", "hi-IN", "en-IN"),
        ("Please tell me the delivery details", "hi-IN", "en-IN"),
        ("मुझे डिलीवरी का समय बताइए", "en-IN", "hi-IN"),
        ("मुझे डिलीवरी का समय बताइए", "hi-en", "hi-en"),
        ("मैंने अभी order किया, order और delivery date अलग होगी ना?", "hi-IN", "hi-en"),
    ],
)
def test_conversation_language_uses_context_without_short_turn_flip(text, previous, expected):
    assert conversation_language(text, previous) == expected


def test_action_examples_use_current_order_facts_and_provider_output_shape(db, scripted_llm):
    llm = scripted_llm([reply("RESPOND", "Hello, how can I help?")])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    run(planner, call.id, "Hello")
    context = json.loads(llm.requests[0]["user_prompt"])
    examples = context["decision_examples"]
    actions = []
    for item in examples:
        decision = PlannerDecision.model_validate(item["decision"])
        actions.append(decision.action.value)
        if decision.proposal:
            assert (
                item["decision"]["proposal"]["primary"]["window_start"]
                == context["order"]["scheduled_window"]["start"]
            )
            assert set(item["decision"]["proposal"]) == {"primary", "fallback"}
            assert "notes" not in item["decision"]["proposal"]["primary"]
            assert decision.message in context["proposal_wording_templates"]
    assert actions == ["PROPOSE_PLAN", "PROPOSE_PLAN", "RESPOND"]


@pytest.mark.parametrize("text", ["What is my delivery window?", "डिलीवरी कब आएगी?", "Delivery kab hai?"])
def test_window_question_example_keeps_server_fact_placeholder(db, scripted_llm, text):
    llm = scripted_llm([reply("RESPOND", "Hello")])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    run(planner, call.id, text)
    context = json.loads(llm.requests[0]["user_prompt"])
    answers = [
        item["decision"]
        for item in context["decision_examples"]
        if item["customer_utterance"] == "What is my scheduled delivery window?"
    ]
    assert len(answers) == 1
    answer = PlannerDecision.model_validate(answers[0])
    assert answer.action.value == "RESPOND"
    assert answer.proposal is None
    assert answer.message in context["response_fact_templates"]
    assert "{order_window}" in answer.message


@pytest.mark.parametrize(
    "text,message,language",
    [
        (
            "Please hand the parcel to me",
            "Understood. {plan_summary} {confirmation_question}",
            "en-IN",
        ),
        ("मुझे ही पार्सल देना", "समझ गई। {plan_summary} {confirmation_question}", "hi-IN"),
        ("Mujhe hi parcel dena", "Samajh gayi. {plan_summary} {confirmation_question}", "hi-en"),
    ],
)
def test_model_wrapper_uses_server_facts_and_records_source(
    db, scripted_llm, text, message, language
):
    planner = PlannerService(db, scripted_llm([reply("PROPOSE_PLAN", message, valid_proposal())]))
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert result.planner.wording_source == "model"
    assert result.planner.fallback_reason is None
    assert result.planner.response_language == language
    if language == "hi-IN":
        assert "शाम 6" in result.agent_message and "रात 8" in result.agent_message
    else:
        assert "6 PM" in result.agent_message and "8 PM" in result.agent_message
    assert "{" not in result.agent_message
    assert result.turn.verdict.valid and not result.turn.customer_confirmed
    agent = list(db.exec(select(Turn).where(Turn.session_id == call.id)).all())[-1]
    assert json.loads(agent.structured_payload_json)["planner"]["wording_source"] == "model"
    event = [e for e in planner.calls.list_events(call.id) if e.event_type == "ASSISTANT_MESSAGE"][
        -1
    ]
    assert json.loads(event.payload_json)["response_language"] == language


@pytest.mark.parametrize(
    "message,reason",
    [
        ("", "EMPTY_MESSAGE"),
        ("x" * 300, "MESSAGE_TOO_LONG"),
        ("Your OTP is 123456. {plan_summary} {confirmation_question}", "UNSAFE_WORDING"),
        ("Understood. {plan_summary}", "INVALID_PLACEHOLDERS"),
        ("Leave it with security. {plan_summary} {confirmation_question}", "UNRECOGNIZED_WRAPPER"),
        (
            "Understood, tomorrow at ten. {plan_summary} {confirmation_question}",
            "UNRECOGNIZED_WRAPPER",
        ),
        ("Samajh gayi. {plan_summary} {confirmation_question}", "LANGUAGE_MISMATCH"),
        ("Understood. {plan_summary.__class__} {confirmation_question}", "INVALID_PLACEHOLDERS"),
    ],
)
def test_unsafe_or_unknown_wrapper_falls_back_without_discarding_valid_plan(
    db, scripted_llm, message, reason
):
    planner = PlannerService(db, scripted_llm([reply("PROPOSE_PLAN", message, valid_proposal())]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "Please hand it directly to me")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == reason
    assert result.turn.verdict.valid
    assert not result.turn.customer_confirmed
    assert "123456" not in result.agent_message
    assert "6 PM" in result.agent_message and "8 PM" in result.agent_message


def test_invalid_policy_always_uses_deterministic_refusal(db, scripted_llm):
    proposal = valid_proposal()
    proposal["primary"]["action"] = "LEAVE_WITH_SECURITY"
    planner = PlannerService(
        db,
        scripted_llm(
            [reply("PROPOSE_PLAN", "Understood. {plan_summary} {confirmation_question}", proposal)]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "Please leave it with security")
    assert not result.turn.verdict.valid
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "POLICY_REJECTED"
    assert "direct handoff" in result.agent_message
    assert not result.turn.customer_confirmed


@pytest.mark.parametrize(
    "text,message,language",
    [
        ("Hello", "Hello, how can I help with your delivery?", "en-IN"),
        ("Can you hear me?", "Yes, I can hear you. What would you like to arrange?", "en-IN"),
        ("क्या आपको मेरी आवाज सुनाई दे रही है?", "हाँ, आपकी आवाज सुनाई दे रही है। बताइए।", "hi-IN"),
        ("Kya meri aawaz aa rahi hai?", "Haan, aapki aawaz aa rahi hai. Boliye.", "hi-en"),
    ],
)
def test_respond_is_natural_and_never_creates_a_plan(db, scripted_llm, text, message, language):
    planner = PlannerService(db, scripted_llm([reply("RESPOND", message)]))
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert result.agent_message == message
    assert result.planner.action.value == "RESPOND"
    assert result.planner.wording_source == "model"
    assert result.planner.response_language == language
    assert result.state == "DISCOVERY" and result.turn.verdict is None
    assert db.exec(select(PlanProposalRecord)).first() is None


@pytest.mark.parametrize("text", ["helo", "Kya aapko meri aawaz aa rhi hai"])
def test_wrong_plan_action_for_greeting_or_audio_check_is_blocked(db, scripted_llm, text):
    planner = PlannerService(
        db,
        scripted_llm(
            [reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", valid_proposal())]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert result.planner.action.value == "RESPOND"
    assert result.planner.fallback_reason == "NON_DELIVERY_INPUT"
    assert result.turn.verdict is None
    assert db.exec(select(PlanProposalRecord)).first() is None


@pytest.mark.parametrize(
    "message",
    [
        "The parcel was delivered successfully.",
        "Security drop is allowed.",
        "ओटीपी बताइए।",
        "सिक्योरिटी को दे दिया।",
        "Your OTP is 123456.",
        "I am a human delivery assistant.",
        "Tell me your verification code.",
    ],
)
def test_respond_cannot_smuggle_otp_or_delivery_promises(db, scripted_llm, message):
    planner = PlannerService(db, scripted_llm([reply("RESPOND", message)]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "Can you help me?")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason in {"UNSAFE_WORDING", "LANGUAGE_MISMATCH"}
    assert "123456" not in result.agent_message
    assert not result.turn.customer_confirmed


def test_question_clears_previous_confirmation_and_bare_yes_cannot_commit(db, scripted_llm):
    planner = PlannerService(
        db,
        scripted_llm(
            [
                reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", valid_proposal()),
                reply("RESPOND", "I can help you review your delivery."),
                reply("CONFIRM_PLAN", "Okay."),
            ]
        ),
    )
    call = start_call(planner.calls)
    run(planner, call.id, "Please hand it to me")
    assert run(planner, call.id, "yes").turn.customer_confirmed
    assert not run(planner, call.id, "Can you help me review it?").turn.customer_confirmed
    result = run(planner, call.id, "yes")
    assert not result.turn.customer_confirmed
    assert result.planner.error_code == "CONFIRMATION_WITHOUT_OFFER"


def test_degraded_proposal_does_not_make_stale_plan_confirmable(db, scripted_llm):
    planner = PlannerService(
        db,
        scripted_llm(
            [
                reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", valid_proposal()),
                reply(
                    "PROPOSE_PLAN",
                    "{plan_summary} {confirmation_question}",
                    valid_proposal(),
                    confidence=0.1,
                ),
                reply("CONFIRM_PLAN", "Okay."),
            ]
        ),
    )
    call = start_call(planner.calls)
    run(planner, call.id, "Please hand it to me")
    run(planner, call.id, "Maybe another time")
    result = run(planner, call.id, "yes")
    assert not result.turn.customer_confirmed
    assert result.planner.error_code == "CONFIRMATION_WITHOUT_OFFER"


def test_rejected_plan_cannot_be_reoffered_unchanged(db, scripted_llm):
    outcome = reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", valid_proposal())
    planner = PlannerService(db, scripted_llm([outcome, outcome]))
    call = start_call(planner.calls)
    run(planner, call.id, "Please hand it to me")
    result = run(planner, call.id, "No, I reject that plan")
    assert result.planner.error_code == "REPEATED_REJECTED_PLAN"
    assert not result.turn.customer_confirmed
    assert any(e.event_type == "PLAN_REJECTED" for e in planner.calls.list_events(call.id))


def test_clarification_limit_keeps_session_recoverable(db, scripted_llm):
    planner = PlannerService(
        db,
        scripted_llm(
            [reply("ASK_CLARIFICATION", "Which delivery window suits you?")]
            * (MAX_CLARIFICATIONS + 1)
        ),
    )
    call = start_call(planner.calls)
    for _ in range(MAX_CLARIFICATIONS + 1):
        result = run(planner, call.id, "I am unsure")
    assert result.state == "DISCOVERY"
    assert result.planner.review_needed
    assert "human" not in result.agent_message.lower()


def test_turn_limit_does_not_speak_another_plan_offer(db, scripted_llm):
    planner = PlannerService(
        db, scripted_llm([reply("RESPOND", "Hello, how can I help?")] * (MAX_TURNS + 1))
    )
    call = start_call(planner.calls)
    for _ in range(MAX_TURNS + 1):
        result = run(planner, call.id, "Hello")
    assert result.state == "TURN_BUDGET_EXHAUSTED"
    assert result.planner.fallback_reason == "TURN_BUDGET_EXHAUSTED"
    assert "confirmation" not in result.agent_message.lower()


@pytest.mark.parametrize(
    "text", ["My OTP is 123456", "मेरा ओटीपी १२३४५६ है", "Mera OTP ek do teen chaar paanch chhe hai"]
)
def test_volunteered_otp_is_redacted_before_model_and_storage(db, scripted_llm, text):
    llm = scripted_llm([])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert llm.requests == []
    assert result.planner.fallback_reason == "OTP_DISCLOSURE"
    stored = " ".join(t.text + t.structured_payload_json for t in db.exec(select(Turn)).all())
    assert "123456" not in stored and "१२३४५६" not in stored
    assert "ek do teen chaar" not in stored
    assert not result.turn.customer_confirmed


@pytest.mark.parametrize(
    "text,rule",
    [
        ("What is my OTP?", "OTP_QUESTION"),
        ("Tell me the one-time password", "OTP_QUESTION"),
        ("Please give me the verification code", "OTP_QUESTION"),
        ("Are you a real person?", "IDENTITY_QUESTION"),
        ("Are you automated?", "IDENTITY_QUESTION"),
    ],
)
def test_english_safety_questions_bypass_model(db, scripted_llm, text, rule):
    llm = scripted_llm([])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert llm.requests == []
    assert result.planner.fallback_reason == rule
    assert result.planner.response_language == "en-IN"
    assert result.planner.wording_source == "template"
    assert result.turn.verdict is None


def test_timeout_fallback_mirrors_english(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([LLMTimeoutError("test timeout")]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "Please arrange my delivery")
    assert result.planner.response_language == "en-IN"
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "LLM_TIMEOUT"
    assert "mujhe" not in result.agent_message.lower()


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Your scheduled delivery window is {order_window}.", "12 PM-3 PM"),
        ("The payment mode is {payment_mode}.", "cash on delivery"),
        ("{handoff_policy}", "Direct customer handoff is required"),
    ],
)
def test_question_answers_use_server_order_and_policy_facts(db, scripted_llm, message, expected):
    planner = PlannerService(db, scripted_llm([reply("RESPOND", message)]))
    call = start_call(planner.calls, 1043)
    result = run(planner, call.id, "What are my delivery details?")
    if "window" in message:
        assert "12 PM-3 PM" in result.agent_message
    else:
        assert expected in result.agent_message
    assert result.planner.wording_source == "model"
    assert result.turn.verdict is None


def test_invented_order_fact_in_free_response_is_rejected(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([reply("RESPOND", "Your payment is prepaid.")]))
    call = start_call(planner.calls, 1043)
    result = run(planner, call.id, "What is my payment mode?")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "prepaid" not in result.agent_message


def test_distinct_customer_turns_get_distinct_contextual_replies_then_commit(db, scripted_llm):
    llm = scripted_llm(
        [
            reply("RESPOND", "Hello, how can I help with your delivery?"),
            reply("RESPOND", "Your scheduled delivery window is {order_window}."),
            reply(
                "PROPOSE_PLAN",
                "Thanks for clarifying. {plan_summary} {confirmation_question}",
                valid_proposal(),
            ),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    greeting = run(planner, call.id, "Hello")
    answer = run(planner, call.id, "What is my delivery window?")
    offered = run(
        planner,
        call.id,
        "Please hand it directly to me and retry after fifteen minutes if unreachable",
    )
    assert greeting.agent_message != answer.agent_message != offered.agent_message
    assert "6 PM-8 PM" in answer.agent_message
    assert offered.planner.wording_source == "model"
    assert '"recent_turns"' in llm.requests[-1]["user_prompt"]
    assert "Hello, how can I help" in llm.requests[-1]["user_prompt"]
    assert run(planner, call.id, "yes").turn.customer_confirmed
    assert planner.calls.commit_plan(call.id, "contextual-demo-commit").state == "COMMITTED"


def test_unknown_named_recipient_is_not_persisted(db, scripted_llm):
    proposal = valid_proposal()
    proposal["primary"].update(action="LEAVE_WITH_NEIGHBOR", named_recipient="Invented Recipient")
    planner = PlannerService(
        db,
        scripted_llm([reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", proposal)]),
    )
    call = start_call(planner.calls, 1046)
    result = run(planner, call.id, "Please give it to my neighbour")
    assert result.planner.error_code == "UNGROUNDED_PLAN_TEXT"
    assert db.exec(select(PlanProposalRecord)).first() is None


def test_named_recipient_and_window_are_rendered_from_validated_proposal(db, scripted_llm):
    proposal = valid_proposal()
    proposal["primary"].update(
        action="LEAVE_WITH_NEIGHBOR",
        named_recipient="Sharma ji",
        window_start="2026-11-05T14:00:00+05:30",
        window_end="2026-11-05T16:00:00+05:30",
    )
    planner = PlannerService(
        db,
        scripted_llm(
            [reply("PROPOSE_PLAN", "Samajh gayi. {plan_summary} {confirmation_question}", proposal)]
        ),
    )
    call = start_call(planner.calls, 1046)
    result = run(planner, call.id, "Sharma ji ko parcel de dena")
    assert result.turn.verdict.valid
    assert result.planner.wording_source == "model"
    assert "Sharma ji" in result.agent_message
    assert "2 PM-4 PM" in result.agent_message


def test_recipient_text_cannot_insert_an_otp_request_into_server_wording(db, scripted_llm):
    proposal = valid_proposal()
    proposal["primary"].update(action="LEAVE_WITH_NEIGHBOR", named_recipient="Your OTP please")
    planner = PlannerService(
        db,
        scripted_llm([reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", proposal)]),
    )
    call = start_call(planner.calls, 1046)
    result = run(planner, call.id, "The neighbour named Your OTP please is available")
    assert result.planner.wording_source == "template"
    assert "Your OTP please" not in result.agent_message
    assert db.exec(select(PlanProposalRecord)).first() is None


def test_continuation_flag_cannot_be_used_to_delay_an_action():
    from pydantic import ValidationError
    value = reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", valid_proposal())
    value["awaiting_continuation"] = True
    with pytest.raises(ValidationError, match="Only a clarification"):
        PlannerDecision.model_validate(value)


def test_unfinished_customer_detail_is_marked_without_saving_a_plan(db, scripted_llm):
    value = reply("ASK_CLARIFICATION", "What is your neighbour's complete house number?")
    value["awaiting_continuation"] = True
    planner = PlannerService(db, scripted_llm([value]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "My neighbour lives at house number four zero")
    assert result.planner.awaiting_continuation
    assert result.planner.wording_source == "model"
    assert not result.turn.customer_confirmed
    assert planner.calls.get_call(call.id).current_proposal_id is None


def fixed_window(db):
    order = db.get(Order, 1042)
    order.window_start = datetime.fromisoformat("2026-11-05T10:00:00+05:30")
    order.window_end = datetime.fromisoformat("2026-11-05T12:00:00+05:30")
    db.add(order)
    db.commit()
    return order


def test_response_placeholder_is_filled_with_server_facts(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "आपकी डिलीवरी का निर्धारित समय {order_window} है।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "model"
    assert result.planner.fallback_reason is None
    assert "सुबह 10" in result.agent_message and "दोपहर 12" in result.agent_message
    assert "IST" not in result.agent_message and "भारतीय समय" not in result.agent_message
    assert "{" not in result.agent_message


def test_response_may_repeat_known_order_facts(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "आपका ऑर्डर 1042 है और डिलीवरी 10:00-12:00 में है।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "model"
    assert result.planner.fallback_reason is None
    assert "1042" in result.agent_message


def test_response_with_invented_date_and_time_is_rejected(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "आपकी डिलीवरी 25 दिसंबर को 09:30 बजे है।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "25" not in result.agent_message
    assert "09:30" not in result.agent_message


def test_response_commitment_language_stays_blocked_with_a_placeholder(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "मैं आपका पार्सल पड़ोसी को सौंप दूँगा {order_window} में।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "पड़ोसी" not in result.agent_message


def multi_handoff_order(db):
    order = db.get(Order, 1042)
    order.payment_mode = PaymentMode.PREPAID
    order.safe_drop_allowed = True
    order.security_drop_allowed = True
    order.neighbour_drop_allowed = True
    order.requires_otp = False
    order.declared_value_inr = 500
    order.window_start = datetime.fromisoformat("2026-11-05T10:00:00+05:30")
    order.window_end = datetime.fromisoformat("2026-11-05T12:00:00+05:30")
    db.add(order)
    db.commit()
    return order


def test_response_uses_the_server_handoff_policy(db, scripted_llm):
    multi_handoff_order(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "{handoff_policy} जानकारी दीजिए।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "model"
    assert result.planner.fallback_reason is None
    assert "अनुमत तरीके" in result.agent_message


def test_delivery_commitment_in_english_is_rejected(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "I will give it to the guard at the gate.")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "एक बात बतानी थी")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "guard" not in result.agent_message


def test_delivery_commitment_in_hindi_is_rejected(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "मैं आपका पार्सल पड़ोसी को दे दूँगा।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "पड़ोसी" not in result.agent_message


def test_invented_hindi_month_is_rejected_even_when_the_day_is_known(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "आपकी डिलीवरी 5 दिसंबर को है।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "फ्लैट 5 में रहता हूँ")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "दिसंबर" not in result.agent_message


def test_spelled_out_month_in_a_reply_is_rejected(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "आपकी डिलीवरी दिसंबर में है।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "दिसंबर" not in result.agent_message


def test_known_day_and_month_in_a_reply_is_kept(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "आपकी डिलीवरी 5 नवंबर को तय है।")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "model"
    assert result.planner.fallback_reason is None
    assert "5 नवंबर" in result.agent_message


def test_rejected_reply_fallback_restates_the_scheduled_window(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "I will deliver the parcel myself.")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे एक बात बतानी थी")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "सुबह 10" in result.agent_message and "दोपहर 12" in result.agent_message
    assert "IST" not in result.agent_message


def test_promise_to_hand_the_parcel_to_someone_is_rejected(db, scripted_llm):
    fixed_window(db)
    planner = PlannerService(
        db,
        scripted_llm([reply("RESPOND", "I will hand it to Sharma ji at the gate.")]),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "��?�� �Ϫ����� �Ϫ��υ������? �Ͼ��?")
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "UNSAFE_WORDING"
    assert "Sharma" not in result.agent_message


@pytest.mark.parametrize(
    "text,language,expected",
    [
        ("बारह नवंबर को हो सकता है।", "hi-IN", "12 नवंबर"),
        ("12 नवंबर को शाम 6 से 8 बजे।", "hi-IN", "12 नवंबर"),
        ("12 November works for me", "en-IN", "12 november"),
        ("November 12 works for me", "en-IN", "12 november"),
        ("You may call me later", "en-IN", None),
    ],
)
def test_mentioned_date_extracts_a_day_and_month(text, language, expected):
    assert mentioned_date(text, language) == expected


def test_date_only_utterance_gets_a_date_aware_time_clarification(db, scripted_llm):
    fixed_window(db)
    item = reply("ASK_CLARIFICATION", "डिलीवरी का कौन सा समय आपके लिए ठीक रहेगा?")
    item.update(missing_fields=["time"], request_details=None, awaiting_continuation=False)
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "बारह नवंबर को हो सकता है।")
    assert result.agent_message == "12 नवंबर को कौन सा समय ठीक रहेगा?"
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "DATE_AWARE_CLARIFICATION"


def test_date_only_utterance_gets_a_date_aware_time_clarification_in_english(db, scripted_llm):
    fixed_window(db)
    item = reply("ASK_CLARIFICATION", "Which delivery time works for you?")
    item.update(missing_fields=["time"], request_details=None, awaiting_continuation=False)
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "12 November works for me")
    assert result.agent_message == "What time on 12 November works for you?"
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "DATE_AWARE_CLARIFICATION"


def test_specific_model_clarification_with_a_date_is_kept(db, scripted_llm):
    fixed_window(db)
    item = reply("ASK_CLARIFICATION", "क्या बारह नवंबर को शाम छह बजे ठीक है?")
    item.update(missing_fields=["time"], request_details=None, awaiting_continuation=False)
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "बारह नवंबर को हो सकता है।")
    assert result.agent_message == "क्या बारह नवंबर को शाम छह बजे ठीक है?"
    assert result.planner.wording_source == "model"
    assert result.planner.fallback_reason is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("रात के आठ बजे", (20, 0)),
        ("रात के चार बजे", (4, 0)),
        ("शाम के चार बजे", (16, 0)),
        ("सुबह के आठ बजे", (8, 0)),
        ("8 बजे रात", (20, 0)),
        ("4 PM", (16, 0)),
        ("10 AM", (10, 0)),
        ("20:00", (20, 0)),
        ("चार बजे like दोपहर को हो सकता है, या फिर रात के आठ बजे", None),
        ("6 se 8", None),
        ("पाँच बजे", None),
    ],
)
def test_mentioned_time_accepts_one_explicit_clock(text, expected):
    assert mentioned_time(text) == expected


def test_spoken_hour_words_can_be_repeated_as_digits(db, scripted_llm):
    fixed_window(db)
    message = "क्या आप 4 बजे दोपहर या 8 बजे रात को डिलीवरी चाहते हैं?"
    item = reply("ASK_CLARIFICATION", message)
    item.update(missing_fields=["time"], request_details=None, awaiting_continuation=False)
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    result = run(
        planner,
        call.id,
        "वैसे चार बजे like दोपहर को हो सकता है, या फिर रात के आठ बजे।",
    )
    assert result.planner.action.value == "ASK_CLARIFICATION"
    assert result.planner.wording_source == "model"
    assert result.agent_message == message


def test_one_clear_time_is_stored_with_the_named_neighbour(db, scripted_llm):
    proposal = valid_proposal()
    proposal["primary"]["action"] = "LEAVE_WITH_NEIGHBOR"
    proposal["primary"]["named_recipient"] = "अरविंद नाम है हमारे भाई का"
    proposal["primary"]["recipient_address"] = "house number four zero three"
    proposal["primary"]["recipient_source"] = {
        "turn_id": None,
        "quote": "अरविंद नाम है हमारे भाई का",
    }
    proposal["primary"]["recipient_address_source"] = {
        "turn_id": None,
        "quote": "house number four zero three",
    }
    follow = reply("ASK_CLARIFICATION", "डिलीवरी का कौन सा समय आपके लिए ठीक रहेगा?")
    follow.update(missing_fields=["time", "recipient"], request_details=None, awaiting_continuation=False)
    planner = PlannerService(
        db,
        scripted_llm(
            [
                reply("PROPOSE_PLAN", "{plan_summary} {confirmation_question}", proposal),
                follow,
            ]
        ),
    )
    call = start_call(planner.calls)
    first = run(
        planner,
        call.id,
        "अरविंद नाम है हमारे भाई का, वो हमारे बगल वाले घर में रहते हैं, house number four zero three में।",
    )
    assert first.turn.verdict is not None and first.turn.verdict.valid
    assert "अरविंद" in first.agent_message and "403" in first.agent_message
    assert "नाम है हमारे भाई" not in first.agent_message
    result = run(planner, call.id, "शाम के छह बजे।")
    assert result.planner.action.value == "PROPOSE_PLAN"
    assert "कौन सा समय" not in result.agent_message
    record = db.get(PlanProposalRecord, planner.calls.get_call(call.id).current_proposal_id)
    stored = json.loads(record.proposal_json)
    assert stored["primary"]["named_recipient"] == "अरविंद"
    assert stored["primary"]["recipient_address"] == "403"
    assert "T18:00" in stored["primary"]["window_start"]
