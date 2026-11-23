"""Regressions for real-provider failures captured in the Edge replay."""

from datetime import date

import pytest
from test_confirmation_conversation import laptop_order, response
from test_outbound_dialogue import record_opening
from test_planner import db as planner_database
from test_planner import run, start_call

from app.dialogue import load_details, load_negotiation
from app.models import PaymentMode
from app.planner import PlannerService, plan_result_message
from app.schemas import CourierCard, PlanProposal, PolicyVerdict

db = planner_database


def eligible_order(db):
    order = laptop_order(db)
    order.declared_value_inr = 1000
    order.neighbour_drop_allowed = True
    db.add(order)
    db.commit()
    return order


def test_scheduled_reference_uses_server_date_even_with_agent_annotation(db, scripted_llm):
    laptop_order(db)
    planner = PlannerService(db, scripted_llm([response(
        "RESPOND", information_topics=["AVAILABILITY"], availability_scope="DATE",
        request_details={"date_reference": "SCHEDULED", "requested_date": "2099-01-01",
                         "date_source": {"turn_id": 99999, "quote": "16 November"}},
    )]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "इसी date पर कोई और समय हो सकता है?")
    assert "16 नवंबर" in result.agent_message and "17 नवंबर" not in result.agent_message
    assert not result.planner.error_code
    assert load_negotiation(db, call.id)["discussed_date"] == "2026-11-16"


def test_mixed_information_and_third_party_request_runs_policy_first(db, scripted_llm):
    laptop_order(db)
    planner = PlannerService(db, scripted_llm([response(
        "ASK_CLARIFICATION", information_topics=["AVAILABILITY"],
        missing_fields=["recipient", "recipient_address"], request_details={
            "action": "RESCHEDULE", "handoff_action": "LEAVE_WITH_NEIGHBOR",
            "date_reference": "SCHEDULED", "requested_start_clock": "15:00:00",
            "time_source": {"quote": "दोपहर के तीन बजे"}, "time_reference": "START_ONLY",
        },
    )]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "दोपहर के तीन बजे किसी और को दे सकते हैं?")
    assert any(v.code == "HIGH_VALUE_DIRECT_HANDOFF" for v in result.turn.verdict.violations)
    assert "recipient_address" not in result.planner.missing_fields


def test_name_at_scheduled_time_then_house_completes_without_date_loop(db, scripted_llm):
    eligible_order(db)
    planner = PlannerService(db, scripted_llm([
        response("ASK_CLARIFICATION", missing_fields=["recipient_address"], request_details={
            "date_reference": "SCHEDULED", "action": "LEAVE_WITH_NEIGHBOR",
            "recipient_source": {"quote": "Aviral Mishra"},
        }),
        response("ASK_CLARIFICATION", missing_fields=["recipient_address"], request_details={
            "recipient_address_source": {"quote": "House number 403"},
        }),
    ]))
    call = start_call(planner.calls)
    first = run(planner, call.id, "Give it to my neighbour Aviral Mishra at the scheduled time.")
    assert first.planner.missing_fields == ["recipient_address"]
    second = run(planner, call.id, "House number 403, next door.")
    assert second.turn.verdict.valid
    current = planner._current_proposal(call)["primary"]
    assert current["named_recipient"] == "Aviral Mishra"
    assert "403" in current["recipient_address"]


def test_bad_date_preserves_independent_name_location_and_clock(db, scripted_llm):
    eligible_order(db)
    planner = PlannerService(db, scripted_llm([response(
        "ASK_CLARIFICATION", information_topics=["AVAILABILITY"], missing_fields=["time"],
        request_details={"date_reference": "ABSOLUTE", "requested_date": "2026-11-17",
                         "date_source": {"turn_id": 99999, "quote": "17 November"},
                         "window_start": "2026-11-17T15:00:00+05:30",
                         "time_source": {"quote": "3 PM"}, "action": "LEAVE_WITH_NEIGHBOR",
                         "recipient_source": {"quote": "Aviral"},
                         "recipient_address_source": {"quote": "house 403"}},
    )]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "Give it to Aviral at house 403 at 3 PM.")
    details = load_details(db, call.id)
    assert result.planner.missing_fields == ["date"]
    assert details.recipient_source.quote == "Aviral"
    assert details.recipient_address_source.quote == "house 403"
    assert details.requested_start_clock.hour == 15
    assert details.window_start is None


def test_discussed_date_clock_resolves_and_confirmation_card_is_customer_only(db, scripted_llm):
    order = laptop_order(db)
    planner = PlannerService(db, scripted_llm([
        response("RESPOND", information_topics=["AVAILABILITY"], availability_scope="DATE",
                 request_details={"date_reference": "ABSOLUTE", "requested_date": "2026-11-17",
                                  "date_source": {"quote": "17 November"}}),
        response("ASK_CLARIFICATION", missing_fields=["end_time"], request_details={
            "date_reference": "DISCUSSED", "requested_start_clock": "15:00:00",
            "time_source": {"quote": "3 PM"}, "action": "RESCHEDULE", "time_reference": "START_ONLY",
        }),
    ]))
    call = start_call(planner.calls)
    run(planner, call.id, "Can you deliver on 17 November?")
    result = run(planner, call.id, "That date at 3 PM.")
    assert result.turn.verdict.valid
    run(planner, call.id, "Please proceed")
    card = planner.calls.commit_plan(call.id, "replay-card-test").courier_card
    assert card.window_start.date() == date(2026, 11, 17)
    assert card.recipient_kind == "CUSTOMER"
    assert card.handoff_restrictions
    assert "authorized named neighbour" not in " ".join(card.instructions)
    assert order.payment_mode == PaymentMode.PREPAID


def test_legacy_courier_card_json_remains_readable():
    card = CourierCard.model_validate({
        "order_id": 1, "action": "RESCHEDULE", "window_start": None,
        "window_end": None, "call_on_arrival": True, "instructions": [],
        "generated_at": "2026-11-15T10:00:00Z",
    })
    assert card.recipient_kind is None and card.handoff_restrictions == []


def test_rejected_proposal_wording_still_explains_required_customer_otp():
    plan = PlanProposal.model_validate({"primary": {
        "action": "LEAVE_WITH_NEIGHBOR", "named_recipient": "Sharma ji", "recipient_address": "404",
        "window_start": "2026-11-16T17:00:00+05:30", "window_end": "2026-11-16T18:00:00+05:30",
    }})
    message = plan_result_message(PolicyVerdict(valid=True, violations=[], allowed_alternatives=[]), plan, "en-IN", requires_otp=True)
    assert "verify the OTP directly" in message
    assert "verification fails" in message


def test_new_timestamp_takes_precedence_over_a_retained_clock(db, scripted_llm):
    laptop_order(db)
    planner = PlannerService(db, scripted_llm([
        response("ASK_CLARIFICATION", missing_fields=["end_time"], request_details={
            "date_reference": "SCHEDULED", "requested_start_clock": "15:00:00",
            "time_source": {"quote": "3 PM"}, "time_reference": "START_ONLY", "action": "RESCHEDULE",
        }),
        response("ASK_CLARIFICATION", missing_fields=["end_time"], request_details={
            "date_reference": "SCHEDULED", "window_start": "2026-11-16T17:00:00+05:30",
            "time_source": {"quote": "5 PM"}, "time_reference": "START_ONLY", "action": "RESCHEDULE",
        }),
    ]))
    call = start_call(planner.calls)
    run(planner, call.id, "3 PM please.")
    result = run(planner, call.id, "Actually 5 PM please.")
    assert result.turn.verdict.valid
    assert load_details(db, call.id).window_start.hour == 17
    assert load_details(db, call.id).requested_start_clock.hour == 17


@pytest.mark.parametrize("text", ["Hello, आपको मेरी आवाज़ आ रही है?", "Can you hear me?"])
def test_hearing_fast_path_is_brief_and_does_not_repeat_opening(db, scripted_llm, text):
    planner = PlannerService(db, scripted_llm([]))
    call = start_call(planner.calls)
    record_opening(db, call.id)
    result = run(planner, call.id, text)
    assert len(result.agent_message) < 120
    assert "OTP" not in result.agent_message and "SwiftCart" not in result.agent_message
