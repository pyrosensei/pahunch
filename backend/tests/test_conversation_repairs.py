import json
from datetime import UTC, datetime

import pytest
from test_planner import db as planner_database
from test_planner import decision, run, start_call, valid_proposal

from app.models import DialerEvent, Order
from app.planner import PlannerService, asks_for_otp
from app.schemas import AgentTurnRequest, PlannerAction
from app.speech_text import spoken_text

db = planner_database


class NoModel:
    model_name = "must-not-be-called"

    async def complete_json(self, **kwargs):
        raise AssertionError("Known order facts need no model")


def test_spoken_hindi_clock_preserves_exact_date_and_minutes():
    source = "डिलीवरी 2 नवंबर 18:52–20:52 भारतीय समय में तय है।"
    result = spoken_text(source, "hi-IN")
    assert "दो नवंबर शाम छह बजकर बावन मिनट से रात आठ बजकर बावन मिनट" in result
    assert ":" not in result
    assert source == "डिलीवरी 2 नवंबर 18:52–20:52 भारतीय समय में तय है।"


@pytest.mark.parametrize("source", ["02 Nov 18:52-20:52 IST", "2 November 18:52–20:52 IST"])
def test_spoken_english_clock_uses_full_month_and_twelve_hour_time(source):
    result = spoken_text(source, "en-IN")
    assert "two November" in result
    assert "six fifty two in the evening to eight fifty two in the evening" in result
    assert "Indian Standard Time" in result


def test_spoken_dates_do_not_rewrite_house_or_order_identifiers():
    assert spoken_text("House 404, order 1054. 05 Nov 10:00-12:00 IST", "en-IN").startswith("House 404, order 1054.")
    assert "ten o'clock in the morning to twelve o'clock in the afternoon" in spoken_text("05 Nov 10:00-12:00 IST", "en-IN")


@pytest.mark.parametrize("text", ["डिलीवरी वाले को OTP देना है", "courier ko OTP chahiye to main de dunga", "Does the courier need the OTP?"])
def test_courier_otp_reference_is_not_a_request_for_assistant_to_reveal_code(text):
    assert not asks_for_otp(text)


def test_courier_otp_guidance_explains_direct_handoff_without_reflexive_refusal(db):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls)
    result = run(planner, call.id, "डिलीवरी वाले को ओटीपी देना है, आपको नहीं")
    assert result.planner.fallback_reason == "COURIER_OTP_GUIDANCE"
    assert "कूरियर को देना है" in result.agent_message
    assert "कभी नहीं" not in result.agent_message
    assert not result.turn.customer_confirmed


def test_english_customer_receives_english_courier_guidance(db):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls)
    result = run(planner, call.id, "Does the courier need my OTP?")
    assert result.planner.response_language == "en-IN"
    assert "delivery OTP is for the courier" in result.agent_message
    assert not any("\u0900" <= char <= "\u097f" for char in result.agent_message)


def test_actual_code_is_still_redacted_before_courier_guidance(db):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls)
    result = run(planner, call.id, "The courier can use my OTP 123456")
    assert result.planner.fallback_reason == "OTP_DISCLOSURE"
    turns = planner._recent_turns(call.id)
    assert "123456" not in json.dumps(turns)


@pytest.mark.parametrize("text,name", [
    ("Sharma ji padosi hain house 404, parcel unhe de do; courier ko OTP phone par bata dunga", "Sharma ji"),
    ("शर्मा जी हाउस नंबर 404 को दे दीजिए, OTP चाहिए तो मुझे कॉल कर दीजिए", "शर्मा जी"),
    ("Give the parcel to my neighbour Sharma at house 404; call me if he needs the OTP", "Sharma"),
])
def test_neighbour_with_otp_requires_courier_verification_and_confirmation(db, scripted_llm, text, name):
    proposal = valid_proposal()
    proposal["primary"]["action"] = "LEAVE_WITH_NEIGHBOR"
    proposal["primary"]["named_recipient"] = name
    llm = scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal=proposal)])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert len(llm.requests) == 1
    assert result.turn.verdict and result.turn.verdict.valid
    assert result.state == "PENDING_CONFIRMATION"
    assert result.planner.fallback_reason is None
    assert not result.turn.customer_confirmed


def test_order_and_delivery_date_question_uses_actual_creation_record(db):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls)
    db.add(DialerEvent(order_id=1042, event_type="ORDER_CREATED", reason="Demo order", payload_json="{}", created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC)))
    db.commit()
    result = run(planner, call.id, "मैंने अभी order किया, order और delivery date अलग होगी ना?")
    assert "02 November 17:30 IST" in result.agent_message
    assert "05 November" in result.agent_message
    assert "alag hain" in result.agent_message
    assert result.planner.response_language == "hi-en"
    assert result.planner.fallback_reason == "ORDER_DATE_QUESTION"


def test_combined_date_and_time_question_returns_both_without_generic_apology(db):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls)
    result = run(planner, call.id, "मुझे मेरा date बताइए time")
    assert "5 नवंबर" in result.agent_message and "18:00-20:00" in result.agent_message
    assert "क्षमा" not in result.agent_message


def test_valid_model_wrapper_does_not_emit_understanding_filler(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "Understood. {plan_summary} {confirmation_question}", proposal=valid_proposal())]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "Please hand the parcel directly to me")
    assert result.turn.verdict.valid
    assert "Understood" not in result.agent_message
    assert "18:00" in result.agent_message


def test_new_order_tomorrow_is_relative_to_actual_call_day_not_delivery_day(db):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls)
    call.started_at = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    db.add(call)
    order = db.get(Order, call.order_id)
    order.window_start = datetime.fromisoformat("2026-11-03T10:00:00+05:30")
    order.window_end = datetime.fromisoformat("2026-11-03T12:00:00+05:30")
    db.add(order)
    db.add(DialerEvent(order_id=order.id, event_type="ORDER_CREATED", reason="Demo order", payload_json="{}", created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC)))
    db.commit()
    context = json.loads(planner._user_prompt(call, order, [], AgentTurnRequest(text="Tomorrow morning is fine")))
    assert context["call_date"] == "2026-11-02"
    assert context["order"]["scheduled_window"]["start"].startswith("2026-11-03")


def test_translated_neighbour_address_still_explains_otp_policy_without_saving_name(db, scripted_llm):
    proposal = valid_proposal()
    proposal["primary"]["action"] = "LEAVE_WITH_NEIGHBOR"
    proposal["primary"]["named_recipient"] = "Sharma ji, house number 404"
    planner = PlannerService(db, scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal=proposal)]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "मेरे पड़ोसी शर्मा जी house number चार सौ चार में रहते हैं। उन्हें दे दीजिए। OTP चाहिए तो मुझे call कर दीजिए।")
    assert result.planner.error_code == "UNGROUNDED_PLAN_TEXT"
    assert result.planner.fallback_reason == "RECIPIENT_SOURCE_REQUIRED"
    assert result.planner.action == PlannerAction.ASK_CLARIFICATION
    assert "404" not in result.agent_message
    assert result.turn.verdict is None
    assert not planner.calls.get_call(call.id).current_proposal_id
    assert not result.turn.customer_confirmed


def test_ungrounded_recipient_never_becomes_a_plan_when_neighbour_drop_is_allowed(db, scripted_llm):
    order = db.get(Order, 1042)
    order.requires_otp = False
    db.add(order)
    db.commit()
    proposal = valid_proposal()
    proposal["primary"]["action"] = "LEAVE_WITH_NEIGHBOR"
    proposal["primary"]["named_recipient"] = "An invented neighbour"
    planner = PlannerService(db, scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal=proposal)]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "मेरे पड़ोसी को दे दीजिए")
    assert result.planner.fallback_reason == "RECIPIENT_SOURCE_REQUIRED"
    assert result.turn.verdict is None
    assert not planner.calls.get_call(call.id).current_proposal_id
    assert "invented" not in result.agent_message
