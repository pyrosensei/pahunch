"""Regressions from the recorded order-1075 conversation."""

import json
from datetime import date, datetime

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import run, start_call

from app.availability import availability_reply
from app.dialogue import load_details, load_negotiation
from app.models import DeliveryCommit, DeliverySlot, Order, PaymentMode
from app.planner import PlannerService
from app.policy import IST
from app.schemas import PlannerDecision
from app.speech_text import customer_window, spoken_text

db = planner_database
TRANSCRIPT = [
    "मैं ठीक कराऊंगी।",
    "अम, मैं fifteen को available नहीं हूँ actually, तो options क्या क्या हैं?",
    "आप दूसरा time कर दीजिए because मैं इस time available नहीं हूँ।",
    "क्या आप और कोई date है मतलब और कोई date available है या सिर्फ fifteen या sixteen को ही available है?",
    "हाँ, पापा।",
    "ऑलराइट, तो क्या आप और कौन कौन सी डेट्स अवेलेबल है? मेरे को सारी डेट बता दीजिए जिस पर आप डिलीवरी कर सकते हैं।",
    "और अगर कोई और डिलीवरी डेट चाहिए हो, तो कौन सी अवेलेबल है?",
    "आप sixteen को कर दीजिए। actually एक काम कीजिए, पंद्रह तारीख को ना, आप पंद्रह तारीख को तीन बजे कर दीजिए, तीन बजे दोपहर को।",
    "दोपहर के तीन बजे पंद्रह तारीख",
]


def mirror_order(db):
    order = db.get(Order, 1042)
    order.window_start = datetime.fromisoformat("2026-11-15T10:00:00+05:30")
    order.window_end = order.window_start.replace(hour=12)
    order.payment_mode = PaymentMode.COD
    order.requires_otp = False
    for slot in db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order.id)).all():
        db.delete(slot)
    for day in (15, 16):
        start = order.window_start.replace(day=day)
        db.add(DeliverySlot(order_id=order.id, start_at=start, end_at=start.replace(hour=18)))
    db.add(order)
    db.commit()
    return order


def response(action="RESPOND", **fields):
    value = {
        "action": action,
        "message": "{available_windows}",
        "proposal": None,
        "escalation_reason": None,
        "confidence": 0.95,
    }
    value.update(fields)
    return value


def requested_time(text, *, legacy_relative=False):
    details = {
        "date_reference": "ABSOLUTE",
        "requested_date": "2026-11-15",
        "date_source": {"quote": "पंद्रह तारीख"},
        "time_source": {"quote": text},
        "window_start": "2026-11-15T15:00:00+05:30",
        "action": "RESCHEDULE",
    }
    if legacy_relative:
        details["relative_day_source"] = {"quote": "पंद्रह तारीख"}
    return details


def replay_decisions():
    background = response(message="{delivery_purpose} {pending_question}", relevance="OFF_TOPIC")
    all_dates = response(information_topics=["AVAILABILITY"])
    return [
        background,
        response(
            information_topics=["AVAILABILITY"],
            availability_scope="ALTERNATIVES",
            message="Available slots are 15 November and 16 November.",
            request_details={
                "action": "RESCHEDULE",
                "unavailable_dates": ["2026-11-15"],
                "date_source": {"quote": "fifteen"},
                "availability_source": {"quote": "fifteen को available नहीं हूँ"},
            },
        ),
        response(
            "ASK_CLARIFICATION",
            availability_scope="ALTERNATIVES",
            request_details={
                "action": "RESCHEDULE",
                "scheduled_window_available": False,
                "availability_source": {"quote": TRANSCRIPT[2]},
            },
        ),
        all_dates,
        background,
        all_dates,
        all_dates,
        response(
            "ASK_CLARIFICATION",
            message="Which time?",
            missing_fields=["time"],
            request_details=requested_time(TRANSCRIPT[7], legacy_relative=True),
        ),
        response("CONFIRM_PLAN", message="Confirmed."),
    ]


def test_exact_session_replay_reaches_review_and_explicit_confirmation(db, scripted_llm):
    mirror_order(db)
    planner = PlannerService(db, scripted_llm(replay_decisions()))
    call = start_call(planner.calls)
    replies = [run(planner, call.id, text) for text in TRANSCRIPT]
    assert replies[1].planner.fallback_reason == "MISSING_INFORMATION_PLACEHOLDERS"
    assert "16 नवंबर" in replies[1].agent_message
    assert "15 नवंबर" not in replies[1].agent_message
    assert "16 नवंबर" in replies[4].agent_message
    assert "उपलब्ध होंगे" not in replies[4].agent_message
    for index in (3, 5, 6):
        assert "15 नवंबर" in replies[index].agent_message
        assert "16 नवंबर" in replies[index].agent_message
        assert "अलावा कोई date दर्ज नहीं" in replies[index].agent_message
    assert all(not reply.turn.customer_confirmed for reply in replies)
    assert all(reply.planner.error_code is None for reply in replies)
    assert replies[-1].turn.turn_count == 9 and replies[-1].turn.verdict.valid
    assert "दोपहर 3" in replies[-2].agent_message and "5 बजे" in replies[-2].agent_message
    assert "plan confirm" in replies[-1].agent_message
    assert load_details(db, call.id).unavailable_dates == []
    confirmed = run(planner, call.id, "हाँ, इस योजना को आगे बढ़ाइए")
    assert confirmed.turn.customer_confirmed
    result = planner.calls.commit_plan(call.id, "latest-session")
    assert result.courier_card.window_start.astimezone(IST).hour == 15
    assert result.courier_card.window_end.astimezone(IST).hour == 17
    assert planner.calls.commit_plan(call.id, "latest-session").commit_id == result.commit_id
    assert len(db.exec(select(DeliveryCommit)).all()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Which other dates are available?",
        "और कौन सी डेट उपलब्ध है?",
        "Aur kaunsi date available hai?",
        "Tell me every available delivery date",
    ],
)
def test_available_dates_questions_use_typed_topic(db, scripted_llm, text):
    mirror_order(db)
    planner = PlannerService(db, scripted_llm([response(information_topics=["AVAILABILITY"])]))
    call = start_call(planner.calls)
    result = run(planner, call.id, text)
    assert len(planner.llm.requests) == 1
    assert result.planner.information_topics[0].value == "AVAILABILITY"
    assert "16 November" in result.agent_message or "16 नवंबर" in result.agent_message
    assert not result.turn.customer_confirmed
    assert load_negotiation(db, call.id)["focus"] == "RESCHEDULE"


def test_date_is_preserved_when_time_arrives_in_a_follow_up(db, scripted_llm):
    mirror_order(db)
    planner = PlannerService(
        db,
        scripted_llm(
            [
                response(
                    "ASK_CLARIFICATION",
                    message="कौन सा समय?",
                    missing_fields=["time"],
                    request_details={
                        "action": "RESCHEDULE",
                        "date_reference": "ABSOLUTE",
                        "requested_date": "2026-11-16",
                        "date_source": {"quote": "16 November"},
                    },
                ),
                response(
                    "ASK_CLARIFICATION",
                    message="कौन सा समय?",
                    missing_fields=["time"],
                    request_details={},
                ),
            ]
        ),
    )
    call = start_call(planner.calls)
    run(planner, call.id, "16 November")
    result = run(planner, call.id, "दोपहर के तीन बजे")
    assert result.turn.verdict.valid
    details = load_details(db, call.id)
    assert details.window_start.astimezone(IST).day == 16
    assert details.window_start.astimezone(IST).hour == 15


def test_invalid_relative_annotation_retains_the_clock_and_asks_only_for_date(db, scripted_llm):
    mirror_order(db)
    planner = PlannerService(
        db,
        scripted_llm(
            [
                response(
                    "ASK_CLARIFICATION",
                    missing_fields=["time"],
                    request_details={
                        "action": "RESCHEDULE",
                        "window_start": "2026-11-15T15:00:00+05:30",
                        "time_source": {"quote": "तीन बजे"},
                        "relative_day_source": {"quote": "पंद्रह तारीख"},
                    },
                )
            ]
        ),
    )
    call = start_call(planner.calls)
    result = run(planner, call.id, TRANSCRIPT[8])
    assert result.planner.missing_fields == ["date"]
    assert load_details(db, call.id).requested_start_clock.hour == 15
    assert load_details(db, call.id).window_start is None
    assert not result.turn.customer_confirmed


def test_all_recorded_periods_are_listed_without_three_period_truncation(db):
    order = mirror_order(db)
    for day in (17, 18, 19):
        start = order.window_start.replace(day=day)
        db.add(DeliverySlot(order_id=order.id, start_at=start, end_at=start.replace(hour=18)))
    db.commit()
    message = availability_reply(
        order, db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order.id)).all(), "hi-IN"
    )
    assert "19 नवंबर" in message and "शाम 6 बजे" in message


@pytest.mark.parametrize("language", ["hi-IN", "hi-en"])
def test_spoken_window_uses_hindi_dates_and_clock_words(language):
    start = datetime.fromisoformat("2026-11-15T15:00:00+05:30")
    text = spoken_text(customer_window(start, start.replace(hour=17), language), language)
    assert "पंद्रह नवंबर" in text
    assert "तीन बजे" in text and "पाँच बजे" in text
    assert "AM" not in text and "PM" not in text and "18:00" not in text


def test_arbitrary_permission_after_many_turns_never_confirms(db, scripted_llm):
    mirror_order(db)
    outputs = [response(information_topics=["AVAILABILITY"])] * 12
    planner = PlannerService(db, scripted_llm(outputs))
    call = start_call(planner.calls)
    for _ in range(12):
        result = run(planner, call.id, "Just do whatever you want")
    assert result.state == "DISCOVERY" and not result.turn.customer_confirmed
    assert not db.exec(select(DeliveryCommit)).all()


def test_old_request_json_and_terminal_records_remain_readable():
    assert PlannerDecision.model_validate(response()).information_topics == []


def test_pronoun_refusal_does_not_invent_unavailability_for_an_offered_date(db, scripted_llm):
    mirror_order(db)
    first = replay_decisions()[1]
    first["request_details"]["scheduled_window_available"] = False
    second = response(
        information_topics=["AVAILABILITY"],
        availability_scope="ALTERNATIVES",
        request_details={
            "unavailable_dates": ["2026-11-16"],
            "scheduled_window_available": False,
            "availability_source": {"quote": "इस time available नहीं हूँ"},
        },
    )
    planner = PlannerService(db, scripted_llm([first, second]))
    call = start_call(planner.calls)
    run(planner, call.id, TRANSCRIPT[1])
    result = run(planner, call.id, TRANSCRIPT[2])
    assert result.planner.error_code is None
    assert "16 नवंबर" in result.agent_message
    assert load_details(db, call.id).unavailable_dates == [date(2026, 11, 15)]


@pytest.mark.parametrize("hour,end,explicit", [(15, 17, False), (17, 18, False), (15, 18, True)])
def test_backend_completes_start_only_windows_and_preserves_explicit_end(
    db, scripted_llm, hour, end, explicit
):
    mirror_order(db)
    text = "15 November at 3 PM to 6 PM" if explicit else f"15 November at {hour}:00"
    details = requested_time(text)
    details.update(
        date_source={"quote": "15 November"},
        window_start=f"2026-11-15T{hour}:00:00+05:30",
        window_end="2026-11-15T18:00:00+05:30",
        scheduled_window_available=False,
        time_reference="EXPLICIT_WINDOW" if explicit else "START_ONLY",
    )
    if explicit:
        details["end_time_source"] = {"quote": "6 PM"}
    item = response(
        "ASK_CLARIFICATION", message="Which time?", missing_fields=["time"], request_details=details
    )
    planner = PlannerService(db, scripted_llm([item]))
    call = start_call(planner.calls)
    # Persisted refusal can be carried by the model without repeating its evidence.
    from app.dialogue import save_details
    from app.schemas import CustomerEvidence, CustomerTurnRequest, RequestDetails

    previous = planner.calls.submit_turn(
        call.id, CustomerTurnRequest(text="The booked time does not work")
    )
    save_details(
        db,
        call.id,
        previous.turn_id,
        RequestDetails(
            scheduled_window_available=False,
            availability_source=CustomerEvidence(quote="The booked time does not work"),
        ),
    )
    result = run(planner, call.id, text)
    assert result.turn.verdict.valid
    assert load_details(db, call.id).window_end.astimezone(IST).hour == end


def test_hindi_half_past_clock_keeps_the_recorded_part_of_day_and_identifiers():
    result = spoken_text("House 403, 15 नवंबर, दोपहर 3:30 से शाम 5 बजे", "hi-en")
    assert result.startswith("House 403")
    assert "दोपहर तीन बजकर तीस मिनट" in result and "शाम पाँच बजे" in result
    assert "रात" not in result


def test_one_english_month_name_does_not_switch_a_hinglish_time_request_to_english():
    from app.language import conversation_language

    assert conversation_language("Pandrah November dopahar teen baje", "hi-en") == "hi-en"
    assert (
        conversation_language("I would like the parcel on November fifteenth at three PM", "hi-en")
        == "en-IN"
    )


def test_all_dates_question_keeps_a_pending_plan_even_when_model_echoes_its_date(db, scripted_llm):
    mirror_order(db)
    first = response(
        "ASK_CLARIFICATION",
        message="Which time?",
        missing_fields=["time"],
        request_details=requested_time(TRANSCRIPT[8]),
    )
    question = "Besides 15 November, which other dates are available?"
    query = response(
        information_topics=["AVAILABILITY"],
        availability_scope="ALL",
        request_details={
            "requested_date": "2026-11-15",
            "date_reference": "ABSOLUTE",
            "date_source": {"quote": "15 November"},
            "window_start": "2026-11-15T15:00:00+05:30",
        },
    )
    planner = PlannerService(db, scripted_llm([first, query]))
    call = start_call(planner.calls)
    run(planner, call.id, TRANSCRIPT[8])
    proposal_id = call.current_proposal_id
    result = run(planner, call.id, question)
    assert "16 November" in result.agent_message
    assert call.current_proposal_id == proposal_id
    assert load_details(db, call.id).window_start.astimezone(IST).hour == 15
    assert not result.turn.customer_confirmed


def test_date_feasibility_question_does_not_erase_an_earlier_refusal(db, scripted_llm):
    mirror_order(db)
    first = replay_decisions()[1]
    query = response(
        information_topics=["AVAILABILITY"],
        availability_scope="DATE",
        request_details={
            "requested_date": "2026-11-15",
            "date_reference": "ABSOLUTE",
            "date_source": {"quote": "15 November"},
        },
    )
    planner = PlannerService(db, scripted_llm([first, query]))
    call = start_call(planner.calls)
    run(planner, call.id, TRANSCRIPT[1])
    result = run(planner, call.id, "Is 15 November available?")
    assert "15 November is available" in result.agent_message
    details = load_details(db, call.id)
    assert details.unavailable_dates == [date(2026, 11, 15)]
    assert details.requested_date is None and details.window_start is None
    assert call.current_proposal_id is None


def test_all_dates_includes_declined_dates_when_model_carries_refusal(db, scripted_llm):
    mirror_order(db)
    first = replay_decisions()[1]
    query = response(
        information_topics=["AVAILABILITY"],
        availability_scope="ALL",
        request_details={"unavailable_dates": ["2026-11-15"]},
    )
    planner = PlannerService(db, scripted_llm([first, query]))
    call = start_call(planner.calls)
    run(planner, call.id, TRANSCRIPT[1])
    result = run(planner, call.id, "Tell me every available date")
    assert "15 November" in result.agent_message and "16 November" in result.agent_message
    assert load_details(db, call.id).unavailable_dates == [date(2026, 11, 15)]


def test_availability_question_preserves_arrival_call_instruction(db, scripted_llm):
    mirror_order(db)
    details = requested_time(TRANSCRIPT[8])
    details["call_on_arrival"] = True
    first = response("ASK_CLARIFICATION", request_details=details, missing_fields=["time"])
    query = response(information_topics=["AVAILABILITY"], request_details={})
    planner = PlannerService(db, scripted_llm([first, query]))
    call = start_call(planner.calls)
    run(planner, call.id, TRANSCRIPT[8])
    proposal_id = call.current_proposal_id
    result = run(planner, call.id, "What other dates are available?")
    assert result.planner.error_code is None
    assert load_details(db, call.id).call_on_arrival
    assert call.current_proposal_id == proposal_id


def test_voice_diagnostic_contains_actual_tts_text(scripted_llm, scripted_speech):
    from test_realtime import realtime_client, start_session

    from app.gnani import SpeechAudio

    speech = scripted_speech(
        audios=[SpeechAudio(data=b"voice", content_type="audio/wav", latency_ms=1)]
    )
    with realtime_client(
        scripted_llm, llm_outcomes=[response(information_topics=["AVAILABILITY"])], speech=speech
    ) as client:
        sid = start_session(client)
        with client.websocket_connect(f"/ws/sessions/{sid}/voice") as ws:
            ws.receive_json()
            ws.send_json({"type": "scripted_turn", "text": "Which dates are available?"})
            while True:
                event = ws.receive_json()
                assert event["type"] != "error"
                if event["type"] == "agent_audio":
                    break
        events = client.get(f"/api/sessions/{sid}/events").json()
        diagnostic = next(
            json.loads(item["payload_json"])
            for item in events
            if item["event_type"] == "ASSISTANT_SPEECH"
        )
        assert diagnostic["tts_text"] == speech.synthesize_calls[0]
        assert diagnostic["response_language"] == "en-IN"
