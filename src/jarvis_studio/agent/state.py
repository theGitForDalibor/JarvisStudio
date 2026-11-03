"""Agent state definitions for LangGraph execution."""

from collections.abc import Sequence
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """The runtime state of the Jarvis Studio LangGraph agent."""

    # History of messages with additive reducer
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Contextual metadata (session_id, user_id, active run_id, etc.)
    run_context: dict[str, Any]

    # Pending HITL approval information if paused
    pending_approval: dict[str, Any] | None

    # Resolution received from user upon HITL resumption
    approval_decision: dict[str, Any] | None
