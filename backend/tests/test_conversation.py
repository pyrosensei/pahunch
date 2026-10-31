import json

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import run, start_call, valid_proposal

from app.calls import MAX_CLARIFICATIONS, MAX_TURNS
from app.language import phrase
from app.llm import LLMTimeoutError
from app.models import PlanProposalRecord, Turn
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
        ("Namaste", "greeting", "hi-en"),
        ("Can you hear me?", "heard", "en-IN"),
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
    assert actions == ["PROPOSE_PLAN", "PROPOSE_PLAN", "RESPOND", "RESPOND"]


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
    assert "18:00" in result.agent_message and "20:00" in result.agent_message
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
    assert "18:00" in result.agent_message


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


def test_clarification_limit_exits_with_terminal_wording(db, scripted_llm):
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
    assert result.state == "ESCALATED"
    assert result.planner.wording_source == "template"
    assert result.planner.fallback_reason == "ESCALATED"
    assert "human" in result.agent_message.lower()


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
        ("Your scheduled delivery window is {order_window}.", "18:00-20:00"),
        ("The payment mode is {payment_mode}.", "cash on delivery"),
        ("{handoff_policy}", "Direct customer handoff is required"),
    ],
)
def test_question_answers_use_server_order_and_policy_facts(db, scripted_llm, message, expected):
    planner = PlannerService(db, scripted_llm([reply("RESPOND", message)]))
    call = start_call(planner.calls, 1043)
    result = run(planner, call.id, "What are my delivery details?")
    if "window" in message:
        assert "12:00-15:00" in result.agent_message
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
    assert "18:00-20:00" in answer.agent_message
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
    proposal["primary"].update(action="LEAVE_WITH_NEIGHBOR", named_recipient="Sharma ji")
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
    assert "18:00-20:00" in result.agent_message


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
