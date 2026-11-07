import json
from datetime import datetime

from sqlmodel import select
from test_planner import db as planner_database
from test_planner import decision, run, start_call, valid_proposal

from app.calls import MAX_CLARIFICATIONS, CallService
from app.llm import LLMStructuredOutputError
from app.models import AuditEvent, DeliverySlot, Order, PlanProposalRecord, Speaker, Turn
from app.planner import PlannerService
from app.schemas import PlannerAction

db = planner_database


def daytime(db):
    order = db.get(Order, 1042)
    order.window_start = datetime.fromisoformat("2026-11-05T10:00:00+05:30")
    order.window_end = datetime.fromisoformat("2026-11-05T12:00:00+05:30")
    db.add(order)
    db.add(
        DeliverySlot(
            order_id=1042, start_at=order.window_start, end_at=order.window_start.replace(hour=18)
        )
    )
    db.commit()
    return order


def quote(text, turn_id=None):
    return {"turn_id": turn_id, "quote": text}


def test_five_oclock_after_four_clarifications_proposes_and_commits(db, scripted_llm):
    daytime(db)
    clarify = decision(PlannerAction.ASK_CLARIFICATION, "Which time works for you?")
    value = decision(PlannerAction.ASK_CLARIFICATION, "What ending time?", confidence=0.95)
    value.update(
        awaiting_continuation=True,
        missing_fields=["end_time"],
        request_details={
            "window_start": "2026-11-05T17:00:00+05:30",
            "time_source": quote("पाँच बजे"),
        },
    )
    planner = PlannerService(db, scripted_llm([clarify] * 4 + [value]))
    call = start_call(planner.calls)
    for _ in range(4):
        run(planner, call.id, "I am unsure")
    result = run(planner, call.id, "पाँच बजे।")
    assert result.turn.verdict.valid
    assert not result.planner.awaiting_continuation
    assert result.turn.clarification_count == 0
    record = db.get(PlanProposalRecord, planner.calls.get_call(call.id).current_proposal_id)
    plan = json.loads(record.proposal_json)
    assert "17:00" in plan["primary"]["window_start"]
    assert "18:00" in plan["primary"]["window_end"]
    confirmed = run(planner, call.id, "Okay.")
    assert confirmed.turn.customer_confirmed
    assert planner.calls.commit_plan(call.id, "replay-five").state == "COMMITTED"


def test_transliterated_name_uses_customer_quotes_and_preserves_house(db, scripted_llm):
    daytime(db)
    value = valid_proposal()
    value["fallback"] = None
    value["primary"].update(
        action="LEAVE_WITH_NEIGHBOR",
        window_start="2026-11-05T10:00:00+05:30",
        window_end="2026-11-05T12:00:00+05:30",
        named_recipient="Sharma ji",
        recipient_source=quote("शर्मा जी"),
        recipient_address_source=quote("हाउस नंबर 403"),
    )
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(
                    PlannerAction.PROPOSE_PLAN,
                    "{plan_summary} {confirmation_question}",
                    proposal=value,
                )
            ]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "शर्मा जी रहते हैं, हाउस नंबर 403, उन्हें दे दीजिए और courier मुझे कॉल करे।")
    assert result.turn.verdict.valid
    assert "शर्मा जी" in result.agent_message and "403" in result.agent_message
    run(planner, call.id, "जी, यही कर दीजिए।")
    card = planner.calls.commit_plan(call.id, "replay-neighbour").courier_card
    assert any("403" in line for line in card.instructions)
    assert any("verify OTP directly with the customer" in line for line in card.instructions)


def test_evidence_cannot_reference_an_agent_or_another_session(db, scripted_llm):
    daytime(db)
    calls = CallService(db)
    call = start_call(calls)
    agent = Turn(
        session_id=call.id, speaker=Speaker.AGENT, text="शर्मा जी", created_at=call.started_at
    )
    db.add(agent)
    db.commit()
    value = valid_proposal()
    value["primary"].update(
        action="LEAVE_WITH_NEIGHBOR",
        named_recipient="Sharma ji",
        recipient_source=quote("शर्मा जी", agent.id),
    )
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(
                    PlannerAction.PROPOSE_PLAN,
                    "{plan_summary} {confirmation_question}",
                    proposal=value,
                )
            ]
        ),
    )
    result = run(planner, call.id, "मेरे पड़ोसी को दे दीजिए")
    assert result.planner.fallback_reason == "RECIPIENT_SOURCE_NOT_CUSTOMER"
    assert not planner.calls.get_call(call.id).current_proposal_id


def test_verified_name_from_earlier_turn_survives_followup(db, scripted_llm):
    daytime(db)
    first = decision(PlannerAction.ASK_CLARIFICATION, "Which time works?")
    first.update(
        missing_fields=["time"],
        request_details={
            "recipient_source": quote("शर्मा जी"),
            "recipient_address_source": quote("403"),
            "action": "LEAVE_WITH_NEIGHBOR",
        },
    )
    second = decision(PlannerAction.ASK_CLARIFICATION, "What ending time?")
    second.update(
        missing_fields=["end_time"],
        request_details={
            "window_start": "2026-11-05T17:00:00+05:30",
            "time_source": quote("पाँच बजे"),
        },
    )
    planner = PlannerService(db, scripted_llm([first, second]))
    call = start_call(planner.calls)
    run(planner, call.id, "शर्मा जी 403 में रहते हैं")
    result = run(planner, call.id, "पाँच बजे")
    assert result.turn.verdict.valid
    assert "शर्मा जी" in result.agent_message and "403" in result.agent_message
    record = db.get(PlanProposalRecord, planner.calls.get_call(call.id).current_proposal_id)
    assert len(json.loads(record.source_turn_ids_json)) == 2


def test_clarification_inserts_recorded_window_and_language_warning_keeps_topic(db, scripted_llm):
    daytime(db)
    llm = scripted_llm(
        [
            decision(PlannerAction.ASK_CLARIFICATION, "क्या {order_window} का समय आपके लिए ठीक है?"),
            decision(PlannerAction.RESPOND, "Could you hear my last reply?"),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    result = run(planner, call.id, "थोड़ा पहले आएंगे?")
    assert "10:00-12:00" in result.agent_message
    assert result.planner.wording_source == "model"
    result = run(planner, call.id, "Aapki aawaz toot rahi hai, meri baat aapko samajh aa rahi hai?")
    assert "hear my last reply" in result.agent_message
    assert result.planner.language_warning == "LANGUAGE_MISMATCH"
    assert result.planner.wording_source == "model"
    assert not result.turn.customer_confirmed


def test_correction_replaces_verified_name_without_reusing_old_label(db, scripted_llm):
    daytime(db)
    first = decision(PlannerAction.ASK_CLARIFICATION, "Which delivery time?")
    first.update(
        missing_fields=["time"],
        request_details={"recipient_source": quote("शर्मा जी"), "action": "LEAVE_WITH_NEIGHBOR"},
    )
    second = decision(PlannerAction.ASK_CLARIFICATION, "Which delivery time?")
    second.update(missing_fields=["time"], request_details={"recipient_source": quote("वर्मा जी")})
    third = decision(PlannerAction.ASK_CLARIFICATION, "What ending time?")
    third.update(
        missing_fields=["end_time"],
        request_details={
            "window_start": "2026-11-05T17:00:00+05:30",
            "time_source": quote("पाँच बजे"),
        },
    )
    planner = PlannerService(db, scripted_llm([first, second, third]))
    call = start_call(planner.calls)
    run(planner, call.id, "शर्मा जी को दे दीजिए")
    run(planner, call.id, "शर्मा जी नहीं, वर्मा जी को देना")
    result = run(planner, call.id, "पाँच बजे")
    assert result.turn.verdict.valid
    assert "वर्मा जी" in result.agent_message and "शर्मा जी" not in result.agent_message


def test_explicit_language_request_still_gets_language_matched_fallback(db, scripted_llm):
    planner = PlannerService(
        db, scripted_llm([decision(PlannerAction.RESPOND, "आप अपनी बात जारी रखिए।")])
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, "Please reply in English")
    assert result.planner.fallback_reason == "LANGUAGE_MISMATCH"
    assert result.planner.wording_source == "template"
    assert not any("\u0900" <= char <= "\u097f" for char in result.agent_message)


def test_invented_number_and_otp_collection_are_not_accepted_in_questions(db, scripted_llm):
    planner = PlannerService(
        db,
        scripted_llm(
            [
                decision(PlannerAction.ASK_CLARIFICATION, "Does house 999 work?"),
                decision(PlannerAction.ASK_CLARIFICATION, "Please tell me your OTP?"),
            ]
        ),
    )
    call = start_call(planner.calls)
    for text in ("My neighbour can receive it", "Which verification do you need?"):
        result = run(planner, call.id, text)
        assert result.planner.fallback_reason == "UNSAFE_WORDING"
        assert "999" not in result.agent_message and "OTP" not in result.agent_message
        assert not result.turn.customer_confirmed


def test_unrelated_audio_question_does_not_repropose_saved_time(db, scripted_llm):
    daytime(db)
    first = decision(PlannerAction.ASK_CLARIFICATION, "Which ending time?")
    first.update(
        missing_fields=["end_time"],
        request_details={
            "window_start": "2026-11-05T17:00:00+05:30",
            "time_source": quote("पाँच बजे"),
        },
    )
    second = decision(PlannerAction.ASK_CLARIFICATION, "Could you hear my last reply?")
    second.update(missing_fields=["other"], request_details={})
    planner = PlannerService(db, scripted_llm([first, second]))
    call = start_call(planner.calls)
    run(planner, call.id, "पाँच बजे")
    result = run(planner, call.id, "I cannot hear your voice")
    assert result.planner.action == PlannerAction.ASK_CLARIFICATION
    assert "hear my last reply" in result.agent_message
    assert not result.turn.customer_confirmed


def test_current_time_quote_repairs_a_stale_model_turn_id(db, scripted_llm):
    daytime(db)
    previous = decision(PlannerAction.ASK_CLARIFICATION, "Which time?")
    next_turn = decision(PlannerAction.ASK_CLARIFICATION, "Which ending time?")
    planner = PlannerService(db, scripted_llm([previous, next_turn]))
    call = start_call(planner.calls)
    first = run(planner, call.id, "दूसरा समय कर दीजिए")
    next_turn.update(
        missing_fields=["end_time"],
        request_details={
            "window_start": "2026-11-05T17:00:00+05:30",
            "time_source": quote("पाँच बजे", first.turn.turn_id),
        },
    )
    # ScriptedLLM keeps the supplied dictionaries, matching a stale id from
    # the latest persisted turn while the current turn is still unrecorded.
    result = run(planner, call.id, "पाँच बजे")
    assert result.turn.verdict.valid
    assert result.planner.action == PlannerAction.PROPOSE_PLAN


def test_structured_output_failure_preserves_verified_time_details(db, scripted_llm):
    daytime(db)
    value = decision(PlannerAction.ASK_CLARIFICATION, "What ending time?", confidence=0.95)
    value.update(
        awaiting_continuation=True,
        missing_fields=["end_time"],
        request_details={
            "window_start": "2026-11-05T17:00:00+05:30",
            "time_source": quote("पाँच बजे"),
        },
    )
    llm = scripted_llm(
        [
            value,
            LLMStructuredOutputError("provider rejected output", detail="missing required fields: message"),
            LLMStructuredOutputError("provider rejected repair", detail="missing required fields: action"),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    first = run(planner, call.id, "पाँच बजे।")
    assert first.turn.verdict is not None and first.turn.verdict.valid
    assert first.turn.turn_count == 1

    db.expire_all()
    proposal_id = planner.calls.get_call(call.id).current_proposal_id
    assert proposal_id is not None
    details = db.exec(
        select(AuditEvent).where(
            AuditEvent.session_id == call.id,
            AuditEvent.event_type == "REQUEST_DETAILS",
        )
    ).all()
    assert len(details) == 1

    second = run(planner, call.id, "मैंने आपको बता दिया पाँच बजे।")
    assert second.planner.degraded is True
    assert second.planner.error_code == "LLM_STRUCTURED_OUTPUT"
    assert second.turn.turn_count == 2
    assert len(llm.requests) == 3

    db.expire_all()
    current = planner.calls.get_call(call.id)
    assert current.current_proposal_id == proposal_id
    assert not current.customer_confirmed
    assert len(
        db.exec(
            select(AuditEvent).where(
                AuditEvent.session_id == call.id,
                AuditEvent.event_type == "REQUEST_DETAILS",
            )
        ).all()
    ) == 1
    plan = json.loads(db.get(PlanProposalRecord, proposal_id).proposal_json)
    assert "17:00" in plan["primary"]["window_start"]
    assert "18:00" in plan["primary"]["window_end"]


def test_repeated_recipient_quote_with_new_source_reference_is_not_progress(db, scripted_llm):
    daytime(db)

    def ask(turn_id=None):
        item = decision(PlannerAction.ASK_CLARIFICATION, "What is your neighbour's house number?")
        item.update(
            missing_fields=["recipient_address"],
            request_details={
                "action": "LEAVE_WITH_NEIGHBOR",
                "recipient_source": quote("शर्मा जी", turn_id),
            },
        )
        return item

    planner = PlannerService(db, scripted_llm([ask(), ask(999), ask(), ask(999)]))
    call = start_call(planner.calls)
    counts = [
        run(planner, call.id, "शर्मा जी को दे दीजिए").turn.clarification_count
        for _ in range(4)
    ]
    assert counts == [1, 2, 3, MAX_CLARIFICATIONS + 1]
    saved = db.exec(
        select(AuditEvent)
        .where(AuditEvent.session_id == call.id, AuditEvent.event_type == "REQUEST_DETAILS")
        .order_by(AuditEvent.id)
    ).all()
    references = [json.loads(event.payload_json)["recipient_source"]["turn_id"] for event in saved]
    assert len(references) == 4
    assert all(reference is not None for reference in references)
    assert references[0] != references[1]
