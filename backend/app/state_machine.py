from app.errors import DomainError
from app.models import CallSession, CallState

TERMINAL_STATES = {
    CallState.COMMITTED,
    CallState.NO_ANSWER,
    CallState.UNREACHABLE,
    CallState.ESCALATED,
    CallState.TOOL_FAILURE,
    CallState.TURN_BUDGET_EXHAUSTED,
}

ALLOWED_TRANSITIONS = {
    CallState.CREATED: {CallState.CALLING},
    CallState.CALLING: {CallState.CONNECTED, CallState.NO_ANSWER, CallState.TOOL_FAILURE},
    CallState.CONNECTED: {
        CallState.CONTEXT_CONFIRMATION,
        CallState.UNREACHABLE,
        CallState.ESCALATED,
    },
    CallState.CONTEXT_CONFIRMATION: {
        CallState.DISCOVERY,
        CallState.NO_ANSWER,
        CallState.UNREACHABLE,
        CallState.ESCALATED,
    },
    CallState.DISCOVERY: {
        CallState.NEGOTIATION,
        CallState.PENDING_CONFIRMATION,
        CallState.NO_ANSWER,
        CallState.UNREACHABLE,
        CallState.ESCALATED,
        CallState.TURN_BUDGET_EXHAUSTED,
    },
    CallState.NEGOTIATION: {
        CallState.DISCOVERY,
        CallState.PENDING_CONFIRMATION,
        CallState.UNREACHABLE,
        CallState.ESCALATED,
        CallState.TURN_BUDGET_EXHAUSTED,
    },
    CallState.PENDING_CONFIRMATION: {
        CallState.NEGOTIATION,
        CallState.COMMITTED,
        CallState.UNREACHABLE,
        CallState.ESCALATED,
        CallState.TURN_BUDGET_EXHAUSTED,
    },
}


def transition(call: CallSession, target: CallState) -> None:
    allowed = ALLOWED_TRANSITIONS.get(call.state, set())
    if target not in allowed:
        raise DomainError(409, f"Illegal call transition: {call.state} -> {target}")
    call.state = target
