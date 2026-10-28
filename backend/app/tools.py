from enum import Enum

from app.errors import DomainError
from app.models import CallState
from app.schemas import PlannerAction


class ToolName(str, Enum):
    GET_ORDER = "get_order"
    GET_AVAILABLE_SLOTS = "get_available_slots"
    GET_DELIVERY_POLICY = "get_delivery_policy"
    PROPOSE_PLAN = "propose_plan"
    COMMIT_PLAN = "commit_plan"
    ESCALATE_TO_HUMAN = "escalate_to_human"


READ_TOOLS = {
    ToolName.GET_ORDER,
    ToolName.GET_AVAILABLE_SLOTS,
    ToolName.GET_DELIVERY_POLICY,
}

STATE_TOOL_PERMISSIONS = {
    CallState.CREATED: {ToolName.GET_ORDER},
    CallState.CALLING: {ToolName.GET_ORDER},
    CallState.CONNECTED: READ_TOOLS | {ToolName.ESCALATE_TO_HUMAN},
    CallState.CONTEXT_CONFIRMATION: READ_TOOLS | {ToolName.ESCALATE_TO_HUMAN},
    CallState.DISCOVERY: READ_TOOLS
    | {ToolName.PROPOSE_PLAN, ToolName.ESCALATE_TO_HUMAN},
    CallState.NEGOTIATION: READ_TOOLS
    | {ToolName.PROPOSE_PLAN, ToolName.ESCALATE_TO_HUMAN},
    CallState.PENDING_CONFIRMATION: READ_TOOLS
    | {ToolName.PROPOSE_PLAN, ToolName.COMMIT_PLAN, ToolName.ESCALATE_TO_HUMAN},
}


PLANNER_ACTION_PERMISSIONS = {
    CallState.DISCOVERY: {
        PlannerAction.ASK_CLARIFICATION,
        PlannerAction.PROPOSE_PLAN,
        PlannerAction.ESCALATE_TO_HUMAN,
    },
    CallState.NEGOTIATION: {
        PlannerAction.ASK_CLARIFICATION,
        PlannerAction.PROPOSE_PLAN,
        PlannerAction.ESCALATE_TO_HUMAN,
    },
    CallState.PENDING_CONFIRMATION: {
        PlannerAction.ASK_CLARIFICATION,
        PlannerAction.PROPOSE_PLAN,
        PlannerAction.CONFIRM_PLAN,
        PlannerAction.ESCALATE_TO_HUMAN,
    },
}


def require_tool(state: CallState, tool: ToolName) -> None:
    if tool not in STATE_TOOL_PERMISSIONS.get(state, set()):
        raise DomainError(409, f"Tool {tool} is not allowed in state {state}")


def require_planner_action(state: CallState, action: PlannerAction) -> None:
    if action not in PLANNER_ACTION_PERMISSIONS.get(state, set()):
        raise DomainError(409, f"Planner action {action} is not allowed in state {state}")
    if action == PlannerAction.PROPOSE_PLAN:
        require_tool(state, ToolName.PROPOSE_PLAN)
    elif action == PlannerAction.ESCALATE_TO_HUMAN:
        require_tool(state, ToolName.ESCALATE_TO_HUMAN)


def allowed_planner_actions(state: CallState) -> set[PlannerAction]:
    return PLANNER_ACTION_PERMISSIONS.get(state, set())
