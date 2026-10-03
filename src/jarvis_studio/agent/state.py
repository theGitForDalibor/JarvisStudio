"""Agent state definitions for LangGraph execution."""

from typing import Annotated, Any, Dict, List, Optional, Sequence
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """The runtime state of the Jarvis Studio LangGraph agent."""

    # History of messages with additive reducer
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Contextual metadata (session_id, user_id, active run_id, etc.)
    run_context: Dict[str, Any]

    # Pending HITL approval information if paused
    pending_approval: Optional[Dict[str, Any]]

    # Resolution received from user upon HITL resumption
    approval_decision: Optional[Dict[str, Any]]
