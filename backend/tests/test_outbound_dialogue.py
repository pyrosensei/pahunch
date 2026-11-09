import asyncio
import json

import pytest
from sqlmodel import select
from test_planner import db as planner_database
from test_planner import start_call
from test_realtime_a1 import voice

from app.calls import CallService, utc_now
from app.errors import DomainError
from app.language import phrase
from app.models import AuditEvent, Speaker, Turn
from app.planner import PlannerService, outbound_opening
from app.schemas import AgentTurnRequest

db = planner_database


class NoModel:
    model_name = "must-not-be-called"

    async def complete_json(self, **kwargs):
        raise AssertionError("Known outbound purpose and availability need no model")


def test_opening_contains_the_actual_order_window_and_relevant_payment_rule(db):
    call = start_call(CallService(db), 1043)
    message, meta = outbound_opening(db, call.id, "en-IN")
    assert "SwiftCart" in message and "1043" in message
    assert "05 November 12:00-15:00 IST" in message
    assert "Payment is due to the courier" in message
    assert "Will you be available" in message
    assert "How can I help" not in message
    assert "OTP" not in message
    assert meta.action.value == "RESPOND"


@pytest.mark.parametrize("utterance", ["क्या बात करनी है आपको?", "हाँ, क्या बात करनी है आपको?", "Why are you calling?", "kya baat karni hai aapko?"])
def test_call_purpose_is_answered_with_delivery_context_not_generic_help(db, utterance):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls, 1043)
    result = asyncio.run(planner.handle_turn(call.id, AgentTurnRequest(text=utterance)))
    assert result.planner.fallback_reason == "OUTBOUND_DELIVERY_PURPOSE"
    assert "1043" in result.agent_message
    assert not result.turn.customer_confirmed
    assert planner.calls.get_call(call.id).current_proposal_id is None
    assert "checking" not in result.agent_message
    assert "जाँच कर रही" not in result.agent_message


def record_opening(db, session_id):
    db.add(
        Turn(
            session_id=session_id,
            speaker=Speaker.AGENT,
            text="opening",
            structured_payload_json=json.dumps({"opening": True}),
            created_at=utc_now(),
        )
    )
    db.commit()


@pytest.mark.parametrize(
    "utterance",
    ["आपको मेरी आवाज़ आ रही?", "Hello, आपको मेरी आवाज़ आ रही है?"],
)
def test_audibility_question_uses_heard_and_delivery_purpose(db, utterance):
    planner = PlannerService(db, NoModel())
    call = start_call(planner.calls, 1042)
    record_opening(db, call.id)
    result = asyncio.run(planner.handle_turn(call.id, AgentTurnRequest(text=utterance)))
    assert result.planner.fallback_reason == "OUTBOUND_DELIVERY_PURPOSE"
    assert result.agent_message.startswith("हाँ, आपकी आवाज सुनाई दे रही है।")
    assert "1042" in result.agent_message
    assert not result.turn.customer_confirmed


def test_prepaid_opening_explains_courier_otp_without_collecting_it(db):
    call = start_call(CallService(db), 1042)
    message, _ = outbound_opening(db, call.id, "en-IN")
    assert "prepaid" in message
    assert "OTP only to the courier" in message
    assert "Tell me your OTP" not in message


def test_socket_records_proactive_opening_and_first_yes_only_proposes():
    async def run():
        async with voice(NoModel()) as (socket, _task, calls, sid):
            socket.control("config", tts_sample_rate=16000, playback_tracking=True)
            introduction = await socket.event("opening")
            assert introduction["opening"] is True
            assert "1042" in introduction["agent_message"]
            end = await socket.event("agent_audio_end")
            turns = calls.db.exec(select(Turn).where(Turn.session_id == sid)).all()
            assert len(turns) == 1 and turns[0].speaker == Speaker.AGENT
            assert json.loads(turns[0].structured_payload_json)["opening"] is True
            assert calls.get_call(sid).turn_count == 0
            event = calls.db.exec(select(AuditEvent).where(AuditEvent.session_id == sid, AuditEvent.event_type == "OPENING_LINE_SPOKEN")).one()
            assert json.loads(event.payload_json)["total_ms"] >= 0
            socket.control("playback_done", generation_id=end["generation_id"])
            socket.control("scripted_turn", text="हाँ, क्या बात करनी है आपको?")
            purpose = await socket.event("turn")
            assert purpose["planner"]["fallback_reason"] == "OUTBOUND_DELIVERY_PURPOSE"
            assert "1042" in purpose["agent_message"]
            assert "क्या आप उस समय" in purpose["agent_message"]
            assert not purpose["turn"]["customer_confirmed"]
            purpose_end = await socket.event("agent_audio_end")
            socket.control("playback_done", generation_id=purpose_end["generation_id"])
            socket.control("scripted_turn", text="हाँ")
            proposed = await socket.event("turn")
            assert proposed["planner"]["action"] == "PROPOSE_PLAN"
            assert proposed["planner"]["wording_source"] == "template"
            assert not proposed["turn"]["customer_confirmed"]
            assert proposed["turn"]["verdict"]["valid"]
            with pytest.raises(DomainError):
                calls.commit_plan(sid, "availability-is-not-consent")
            reply = await socket.event("agent_audio_end")
            socket.control("playback_done", generation_id=reply["generation_id"])
            socket.control("scripted_turn", text="हाँ ठीक है")
            confirmation = await socket.event("turn")
            assert confirmation["planner"]["action"] == "CONFIRM_PLAN"
            assert confirmation["turn"]["customer_confirmed"]
    asyncio.run(run())


@pytest.mark.parametrize("language", ["en-IN", "hi-IN", "hi-en"])
def test_acknowledgement_does_not_claim_unspecified_checking(language):
    assert "check" not in phrase("ack", language).lower()
    assert "जाँच" not in phrase("ack", language)
