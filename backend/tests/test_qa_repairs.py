"""Regressions for failures captured in the November 13 QA replay."""
import json
from datetime import datetime

import pytest
from sqlmodel import select
from test_call_api import client as call_client
from test_planner import db as planner_db
from test_planner import decision, run, start_call, valid_proposal
from test_replay_repairs import daytime, quote

from app.calls import CallService
from app.consent import is_explicit_confirmation
from app.errors import DomainError
from app.language import recipient_label
from app.models import DeliverySlot, PlanProposalRecord, Speaker, Turn
from app.planner import PlannerService
from app.policy import IST, _utc, usable_recipient_location
from app.schemas import CustomerTurnRequest, PlannerAction, PlanProposal

db = planner_db
client = call_client


@pytest.mark.parametrize("text", [
    "Yes, I confirm this plan.", "Yes, please confirm it.", "I agree, please go ahead.",
    "Yes, confirm that date.", "I accept the proposed arrangement.",
    "हाँ, मैं इस योजना की पुष्टि करता हूँ।", "Haan, isi plan ko confirm kar dijiye.",
])
def test_natural_explicit_agreement_is_recognized(text):
    assert is_explicit_confirmation(text)


@pytest.mark.parametrize("text", [
    "Yes, I do not confirm this plan", "I agree if you change the neighbour", "Maybe I agree",
    "हाँ, लेकिन मैं इस योजना की पुष्टि नहीं करता हूँ।", "Yes, but change the time",
])
def test_negation_uncertainty_and_revisions_are_not_agreement(text):
    assert not is_explicit_confirmation(text)


@pytest.mark.parametrize("text,confidence,with_proposal", [
    ("No. I do not agree. Do not confirm this plan.", None, False),
    ("Yes", .1, False), ("Yes", None, True),
])
def test_http_rejects_forged_or_unreliable_confirmation(client, text, confidence, with_proposal):
    sid = client.post("/api/orders/1042/call").json()["id"]
    client.post(f"/api/sessions/{sid}/connect").raise_for_status()
    client.post(f"/api/sessions/{sid}/context-confirmation").raise_for_status()
    client.post(f"/api/sessions/{sid}/turns", json={"text": "Use the recorded window", "proposal": valid_proposal()}).raise_for_status()
    body = {"text": text, "confirmation": True, "transcript_confidence": confidence}
    if with_proposal:
        body["proposal"] = valid_proposal()
    assert client.post(f"/api/sessions/{sid}/turns", json=body).status_code == 409
    assert not client.get(f"/api/sessions/{sid}").json()["customer_confirmed"]
    assert client.post(f"/api/sessions/{sid}/commit", json={"idempotency_key": "qa-blocked-consent"}).status_code == 409


def test_negative_confirmation_invalidates_previous_agreement(db):
    service = CallService(db)
    call = start_call(service)
    service.submit_turn(call.id, CustomerTurnRequest(text="Use this window", proposal=PlanProposal.model_validate(valid_proposal())))
    service.submit_turn(call.id, CustomerTurnRequest(text="Yes", confirmation=True))
    with pytest.raises(DomainError):
        service.submit_turn(call.id, CustomerTurnRequest(text="No, do not confirm", confirmation=True))
    assert not service.get_call(call.id).customer_confirmed
    with pytest.raises(DomainError):
        service.commit_plan(call.id, "qa-retracted-confirmation")


def test_commit_revalidates_saved_confirmation_text(db):
    service = CallService(db)
    call = start_call(service)
    service.submit_turn(call.id, CustomerTurnRequest(text="Use this window", proposal=PlanProposal.model_validate(valid_proposal())))
    service.submit_turn(call.id, CustomerTurnRequest(text="Yes", confirmation=True))
    latest = db.exec(select(Turn).where(Turn.session_id == call.id, Turn.speaker == Speaker.CUSTOMER).order_by(Turn.id.desc())).first()
    latest.text = "No, do not confirm"
    db.commit()
    with pytest.raises(DomainError, match="verified customer confirmation"):
        service.commit_plan(call.id, "qa-old-unverified-consent")


def test_low_confidence_yes_cannot_use_fast_confirmation(db, scripted_llm):
    llm = scripted_llm([decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal=valid_proposal())])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    run(planner, call.id, "Direct handoff in the recorded window")
    result = run(planner, call.id, "Yes", transcript_confidence=.1)
    assert result.planner.error_code == "LOW_TRANSCRIPT_CONFIDENCE"
    assert not result.turn.customer_confirmed
    assert len(llm.requests) == 1
    with pytest.raises(DomainError):
        planner.calls.commit_plan(call.id, "qa-low-confidence-fast-path")


@pytest.mark.parametrize("location", [
    "somewhere nearby", "near my home", "पड़ोस में", "पता नहीं", "anywhere",
    "Ramesh's house", "I do not know", "same building near me", "9000000000",
])
def test_location_requires_a_premises_identifier(location):
    assert not usable_recipient_location(location)


@pytest.mark.parametrize("location", [
    "403", "Flat 404", "House B", "House number four zero three", "हाउस नंबर चार शून्य तीन",
    "Opposite the customer entrance, the house with the blue gate", "मेरे घर के बाएँ नीले गेट वाला मकान",
])
def test_specific_locations_remain_usable(location):
    assert usable_recipient_location(location)


@pytest.mark.parametrize("spoken", [
    "House number four hundred three", "मकान नंबर चार सौ तीन", "चार सौ तीन",
    "four zero three", "house number चार शून्य तीन",
])
def test_spoken_house_identifiers_preserve_hundreds_and_digit_sequences(spoken):
    assert recipient_label("recipient_address", spoken) == "403"


def test_commit_rechecks_neighbour_location(db):
    order = daytime(db)
    service = CallService(db)
    call = start_call(service)
    value = valid_proposal()
    value["primary"].update(action="LEAVE_WITH_NEIGHBOR", named_recipient="Ramesh", recipient_address="403",
                            window_start=order.window_start.isoformat(), window_end=order.window_end.isoformat())
    service.submit_turn(call.id, CustomerTurnRequest(text="Ramesh at house 403", proposal=PlanProposal.model_validate(value)))
    service.submit_turn(call.id, CustomerTurnRequest(text="Yes", confirmation=True))
    record = db.get(PlanProposalRecord, service.get_call(call.id).current_proposal_id)
    value["primary"]["recipient_address"] = "somewhere nearby"
    record.proposal_json = json.dumps(value)
    db.commit()
    with pytest.raises(DomainError, match="current delivery policy"):
        service.commit_plan(call.id, "qa-vague-location-commit")


def test_direct_handoff_change_clears_unresolved_neighbour(db, scripted_llm):
    order = daytime(db)
    first = decision(PlannerAction.ASK_CLARIFICATION, "Where does Ramesh live?")
    first.update(missing_fields=["recipient_address"], request_details={"action": "LEAVE_WITH_NEIGHBOR", "recipient_source": quote("Ramesh")})
    value = valid_proposal()
    start = _utc(order.window_start).astimezone(IST).replace(hour=17, minute=0)
    value["primary"].update(action="RESCHEDULE", window_start=start.isoformat(), window_end=start.replace(hour=18).isoformat())
    second = decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal=value)
    second["request_details"] = {"action": "RESCHEDULE", "handoff_action": "HAND_TO_CUSTOMER", "time_source": quote("five PM"),
                                  "window_start": value["primary"]["window_start"], "window_end": value["primary"]["window_end"]}
    planner = PlannerService(db, scripted_llm([first, second]))
    call = start_call(planner.calls)
    run(planner, call.id, "Give it to Ramesh somewhere nearby")
    result = run(planner, call.id, "Actually deliver directly to me at five PM")
    record = db.get(PlanProposalRecord, planner.calls.get_call(call.id).current_proposal_id)
    assert result.turn.verdict.valid, record.proposal_json
    assert "recipient_address" not in result.planner.missing_fields
    assert run(planner, call.id, "Yes, I confirm this plan.").turn.customer_confirmed
    card = planner.calls.commit_plan(call.id, "qa-direct-handoff-switch").courier_card
    assert card.named_recipient is None and card.recipient_address is None


def test_okay_answers_recorded_model_availability_question(db, scripted_llm):
    item = decision(PlannerAction.RESPOND, "Are you available during {order_window}?")
    item["missing_fields"] = ["availability"]
    llm = scripted_llm([item])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    run(planner, call.id, "Hello, can you hear me?")
    result = run(planner, call.id, "Okay.")
    assert result.turn.verdict.valid
    assert result.planner.action == "PROPOSE_PLAN"
    assert not result.turn.customer_confirmed
    assert len(llm.requests) == 1


@pytest.mark.parametrize("source,offset", [("tomorrow", 1), ("कल", 1), ("today", 0)])
def test_relative_day_is_bound_to_server_call_date(db, scripted_llm, source, offset):
    order = daytime(db)
    correct = _utc(order.window_start).astimezone(IST).replace(day=5 + offset, hour=17, minute=0)
    db.add(DeliverySlot(order_id=order.id, start_at=correct.replace(hour=10), end_at=correct.replace(hour=18)))
    db.commit()
    value = valid_proposal()
    value["primary"].update(action="RESCHEDULE", window_start=correct.replace(day=7).isoformat(), window_end=correct.replace(day=7, hour=18).isoformat())
    item = decision(PlannerAction.PROPOSE_PLAN, "{plan_summary} {confirmation_question}", proposal=value)
    item["request_details"] = {"action": "RESCHEDULE", "relative_day_source": quote(source),
                                "time_source": quote(f"5 PM {source}"), "window_start": value["primary"]["window_start"], "window_end": value["primary"]["window_end"]}
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    result = run(planner, call.id, f"I cannot hear your voice. Please deliver at 5 PM {source}")
    record = db.get(PlanProposalRecord, planner.calls.get_call(call.id).current_proposal_id)
    assert result.turn.verdict.valid, record.proposal_json
    assert datetime.fromisoformat(json.loads(record.proposal_json)["primary"]["window_start"]).date() == correct.date()


def test_compound_date_question_also_answers_payment(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([]))
    call = start_call(planner.calls)
    result = run(planner, call.id, "What date and time is my delivery, and is it already paid?")
    assert "November" in result.agent_message
    assert "already paid" in result.agent_message
