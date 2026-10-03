"""Router logic for LangGraph conditional edges."""

from typing import Literal
from langchain_core.messages import AIMessage
from langgraph.graph import END

from jarvis_studio.agent.state import AgentState
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def route_after_assistant(
    state: AgentState,
    registry: ToolRegistry = None,
) -> Literal["approval_gate", "tools", "__end__"]:
    """Determine the next step after the assistant generates a response."""
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return END

    tool_registry = registry or get_default_registry()

    # Check if any tool call requires HITL approval
    for call in last_message.tool_calls:
        tool_name = call.get("name")
        if tool_registry.requires_approval(tool_name):
            return "approval_gate"

    return "tools"
