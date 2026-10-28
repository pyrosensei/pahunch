import asyncio
from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.calls import CallService
from app.errors import DomainError
from app.llm import LLMTimeoutError
from app.models import (
    AuditEvent,
    CallState,
    PlanProposalRecord,
    Speaker,
    Turn,
)
from app.planner import PlannerService, _planner_schema, is_explicit_confirmation
from app.schemas import (
    AgentTurnRequest,
    PlannerAction,
    ViolationCode,
)
from app.seed import seed_database

START = datetime.fromisoformat("2026-11-05T18:00:00+05:30")
END = datetime.fromisoformat("2026-11-05T20:00:00+05:30")


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session)
        yield session


def decision(
    action: PlannerAction,
    message: str,
    *,
    proposal: dict | None = None,
    escalation_reason: str | None = None,
    confidence: float = 0.9,
) -> dict:
    return {
        "action": action.value,
        "message": message,
        "proposal": proposal,
        "escalation_reason": escalation_reason,
        "confidence": confidence,
    }


def valid_proposal() -> dict:
    return {
        "primary": {
            "action": "HAND_TO_CUSTOMER",
            "window_start": START.isoformat(),
            "window_end": END.isoformat(),
        },
        "fallback": {
            "action": "RETRY_AFTER_DELAY",
            "delay_minutes": 15,
            "max_retries": 1,
        },
    }


def start_call(service: CallService, order_id: int = 1042):
    call = service.create_call(order_id)
    service.connect(call.id)
    return service.confirm_context(call.id)


def run(
    planner: PlannerService,
    session_id: str,
    text: str,
    transcript_confidence: float | None = None,
):
    return asyncio.run(
        planner.handle_turn(
            session_id,
            AgentTurnRequest(text=text, transcript_confidence=transcript_confidence),
        )
    )


def test_valid_proposal_uses_backend_message_and_waits_for_confirmation(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(
                PlannerAction.PROPOSE_PLAN,
                "Delivery done! Aapka parcel schedule ho gaya.",
                proposal=valid_proposal(),
            )
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "Main 6 se 8 ke beech ghar par hoon, ek retry kar dena")

    assert result.state == CallState.PENDING_CONFIRMATION.value
    assert result.turn.verdict is not None and result.turn.verdict.valid
    assert result.turn.customer_confirmed is False
    assert "direct handoff" in result.agent_message
    assert "done" not in result.agent_message.lower()
    assert result.planner.action == PlannerAction.PROPOSE_PLAN
    assert result.planner.degraded is False

    prompt = llm.requests[0]["user_prompt"]
    assert "2026-11-05T18:00:00+05:30" in prompt
    assert "Main 6 se 8 ke beech ghar par hoon" in prompt
    assert "untrusted" in llm.requests[0]["system_prompt"]

    turns = list(db.exec(select(Turn).where(Turn.session_id == call.id).order_by(Turn.id)).all())
    assert [turn.speaker for turn in turns] == [Speaker.CUSTOMER, Speaker.AGENT]
    assert turns[-1].text == result.agent_message
    events = list(db.exec(select(AuditEvent).where(AuditEvent.session_id == call.id)).all())
    assert any(event.event_type == "LLM_DECISION" for event in events)


def test_invalid_plan_moves_to_negotiation_with_policy_explanation(db, scripted_llm):
    proposal = valid_proposal()
    proposal["fallback"] = {
        "action": "LEAVE_WITH_SECURITY",
        "window_start": START.isoformat(),
        "window_end": END.isoformat(),
    }
    llm = scripted_llm(
        [
            decision(
                PlannerAction.PROPOSE_PLAN,
                "Security me chhod diya.",
                proposal=proposal,
            )
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "Agar phone na uthaun to security ko de dena")

    assert result.state == CallState.NEGOTIATION.value
    assert result.turn.verdict is not None and not result.turn.verdict.valid
    assert any(
        violation.code == ViolationCode.OTP_REQUIRED_NO_THIRD_PARTY
        for violation in result.turn.verdict.violations
    )
    assert "OTP" in result.agent_message
    assert result.turn.customer_confirmed is False
    assert "chhod diya" not in result.agent_message


def test_explicit_confirmation_then_commit(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(PlannerAction.PROPOSE_PLAN, "Plan ready.", proposal=valid_proposal()),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    run(planner, call.id, "6 se 8 baje direct handoff, ek retry")
    confirmed = run(planner, call.id, "Haan, theek hai.")

    assert confirmed.turn.customer_confirmed is True
    assert confirmed.state == CallState.PENDING_CONFIRMATION.value
    assert confirmed.planner.action == PlannerAction.CONFIRM_PLAN
    assert confirmed.planner.model == "rules:explicit-confirmation"
    assert len(llm.requests) == 1

    events = list(db.exec(select(AuditEvent).where(AuditEvent.session_id == call.id)).all())
    assert any(event.event_type == "CONFIRMATION_RULE" for event in events)

    committed = planner.calls.commit_plan(call.id, "planner-confirm-commit-01")
    assert committed.state == CallState.COMMITTED.value


def test_confirmation_with_new_instruction_is_not_accepted(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(PlannerAction.PROPOSE_PLAN, "Plan ready.", proposal=valid_proposal()),
            decision(PlannerAction.CONFIRM_PLAN, "Theek hai."),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)
    run(planner, call.id, "6 se 8 baje direct handoff, ek retry")

    result = run(planner, call.id, "Haan but security me chhod dena")

    assert result.turn.customer_confirmed is False
    assert result.state == CallState.PENDING_CONFIRMATION.value
    assert result.planner.error_code == "EXPLICIT_CONFIRMATION_REQUIRED"
    assert "current_proposal" in llm.requests[1]["user_prompt"]
    assert '"current_state": "PENDING_CONFIRMATION"' in llm.requests[1]["user_prompt"]
    assert "Confirm karne" in result.agent_message


def test_low_confidence_proposal_is_not_executed(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(
                PlannerAction.PROPOSE_PLAN,
                "Shayad 6 se 8.",
                proposal=valid_proposal(),
                confidence=0.2,
            )
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "Kuch time ke aas paas")

    assert result.state == CallState.DISCOVERY.value
    assert result.planner.degraded is True
    assert result.planner.error_code == "LOW_CONFIDENCE"
    assert result.turn.verdict is None
    assert db.exec(select(PlanProposalRecord)).first() is None


@pytest.mark.parametrize(
    ("outcomes", "error_code"),
    [
        ([LLMTimeoutError("provider slow")], "LLM_TIMEOUT"),
        (["not-json", "not-json"], "LLM_INVALID_OUTPUT"),
    ],
)
def test_provider_failures_degrade_without_state_change(db, scripted_llm, outcomes, error_code):
    llm = scripted_llm(outcomes)
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "6 se 8 baje direct handoff")

    assert result.state == CallState.DISCOVERY.value
    assert result.planner.degraded is True
    assert result.planner.error_code == error_code
    assert result.turn.customer_confirmed is False
    events = list(db.exec(select(AuditEvent).where(AuditEvent.session_id == call.id)).all())
    assert any(event.event_type == "LLM_FALLBACK" for event in events)


def test_low_transcript_confidence_cannot_create_a_plan(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(
                PlannerAction.PROPOSE_PLAN,
                "Theek hai.",
                proposal=valid_proposal(),
            )
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "6 se 8 baje direct handoff", transcript_confidence=0.2)

    assert result.state == CallState.DISCOVERY.value
    assert result.planner.error_code == "LOW_TRANSCRIPT_CONFIDENCE"
    assert result.turn.verdict is None
    assert db.exec(select(PlanProposalRecord)).first() is None


def test_low_transcript_confidence_still_allows_a_clarification(db, scripted_llm):
    llm = scripted_llm(
        [decision(PlannerAction.ASK_CLARIFICATION, "Aap 6 se 8 baje aa sakte hain?")]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "6 se 8", transcript_confidence=0.2)

    assert result.state == CallState.DISCOVERY.value
    assert result.planner.action == PlannerAction.ASK_CLARIFICATION
    assert result.planner.degraded is False


def test_confirm_action_is_forbidden_before_a_plan_exists(db, scripted_llm):
    llm = scripted_llm([decision(PlannerAction.CONFIRM_PLAN, "Theek hai.")])
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "Haan theek hai")

    assert result.state == CallState.DISCOVERY.value
    assert result.turn.customer_confirmed is False
    assert result.planner.error_code == "ACTION_NOT_ALLOWED"


def test_escalation_is_acked_with_a_fixed_message(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(
                PlannerAction.ESCALATE_TO_HUMAN,
                "Address update kar diya.",
                escalation_reason="ADDRESS_CHANGE_REQUESTED",
            )
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "Mera address change karna hai")

    assert result.state == CallState.ESCALATED.value
    assert result.planner.action == PlannerAction.ESCALATE_TO_HUMAN
    assert "human team" in result.agent_message
    assert "Address update" not in result.agent_message


def test_unsafe_clarification_message_is_replaced(db, scripted_llm):
    llm = scripted_llm(
        [decision(PlannerAction.ASK_CLARIFICATION, "OTP bata dijiye, delivery done hai.")]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "Aap OTP batao")

    assert result.state == CallState.DISCOVERY.value
    assert "OTP" not in result.agent_message
    assert "done" not in result.agent_message.lower()
    assert "samajh nahi aaya" in result.agent_message


def test_planner_rejects_non_conversation_state(db, scripted_llm):
    llm = scripted_llm([])
    planner = PlannerService(db, llm)
    call = planner.calls.create_call(1042)

    with pytest.raises(DomainError, match="Planner is not available"):
        run(planner, call.id, "hello")
    assert llm.requests == []


@pytest.mark.parametrize(
    "text",
    [
        "haan",
        "Haan, theek hai.",
        "yes please",
        "ok confirm kar do",
        "bilkul",
        "sahi hai, aage badhao",
        "haan, is plan ko aage badhao",
        "हाँ, ठीक है।",
        "हाँ",
        "हाँ जी, कर दो",
        "बिल्कुल",
        "ठीक है",
        "हां, ठीक आहे।",
    ],
)
def test_explicit_confirmation_phrases(text):
    assert is_explicit_confirmation(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nahi",
        "haan mat karo",
        "haan but kal 6 baje",
        "haan 6 se 8",
        "haan but security me chhod dena",
        "haan Sharma ji ko de dena",
        "haan?",
        "address change karna hai",
        "नहीं",
        "हाँ, लेकिन कल 6 बजे",
        "हाँ, पड़ोसी को दे दो",
    ],
)
def test_non_explicit_or_changed_confirmation_phrases(text):
    assert not is_explicit_confirmation(text)


def test_invalid_structured_output_is_repaired_once(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(PlannerAction.PROPOSE_PLAN, "Plan ready."),
            decision(
                PlannerAction.PROPOSE_PLAN,
                "Plan ready.",
                proposal=valid_proposal(),
            ),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "6 se 8 baje direct de dena")

    assert result.planner.degraded is False
    assert result.planner.action == PlannerAction.PROPOSE_PLAN
    assert result.turn.verdict is not None and result.turn.verdict.valid
    assert len(llm.requests) == 2
    assert "failed validation" in llm.requests[1]["user_prompt"]
    assert result.planner.latency_ms == 24
    assert result.planner.completion_tokens == 160
    events = list(db.exec(select(AuditEvent).where(AuditEvent.session_id == call.id)).all())
    assert any(event.event_type == "LLM_REPAIR" for event in events)


def test_failed_repair_degrades_safely(db, scripted_llm):
    llm = scripted_llm(
        [
            decision(PlannerAction.PROPOSE_PLAN, "Plan ready."),
            decision(PlannerAction.PROPOSE_PLAN, "Plan ready."),
        ]
    )
    planner = PlannerService(db, llm)
    call = start_call(planner.calls)

    result = run(planner, call.id, "6 se 8 baje direct de dena")

    assert result.planner.degraded is True
    assert result.planner.error_code == "LLM_INVALID_OUTPUT"
    assert result.turn.verdict is None
    assert "samajh nahi aaya" in result.agent_message


def test_planner_schema_omits_unconsumed_descriptive_fields():
    schema = _planner_schema()
    proposal = schema["$defs"]["PlanProposal"]
    step = schema["$defs"]["PlanStep"]

    for field in ("customer_statements", "notes", "confidence"):
        assert field not in proposal["properties"]
        assert field not in proposal["required"]
    assert "notes" not in step["properties"]
    assert "notes" not in step["required"]
    assert "confidence" in schema["properties"]
    assert "confidence" in schema["required"]
