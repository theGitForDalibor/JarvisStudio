"""Approval gate node leveraging LangGraph interrupt for HITL."""

from typing import Any, Dict
from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    get_hitl_manager,
)
from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def create_approval_node(registry: ToolRegistry = None):
    """Create the approval gate node checking high-risk tool calls."""
    tool_registry = registry or get_default_registry()
    hitl_mgr = get_hitl_manager()

    def approval_node(state: AgentState) -> Dict[str, Any]:
        """Intercept high-risk tool calls and trigger LangGraph interrupt."""
        messages = state.get("messages", [])
        if not messages:
            return {}

        last_message = messages[-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        run_context = state.get("run_context", {})
        thread_id = run_context.get("session_id", "default")
        run_id = run_context.get("run_id", "run_unknown")

        # Find dangerous tool call requiring confirmation
        interrupted_call = None
        for call in last_message.tool_calls:
            tool_name = call.get("name")
            if tool_registry.requires_approval(tool_name):
                interrupted_call = call
                break

        if not interrupted_call:
            return {}

        # Record approval request in manager
        tool_name = interrupted_call.get("name", "unknown")
        args = interrupted_call.get("args", {})
        tool_call_id = interrupted_call.get("id", "")

        appr_req = hitl_mgr.create_request(
            thread_id=thread_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments=args,
            description=f"Action '{tool_name}' requires confirmation",
        )

        approval_payload = {
            "approval_id": appr_req.id,
            "thread_id": thread_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": args,
            "risk_level": appr_req.risk_level.value,
            "description": appr_req.description,
        }

        logger.warning(
            f"Pausing execution for HITL approval: {tool_name} (ID: {appr_req.id})"
        )

        # Trigger native LangGraph interrupt. Execution pauses until resumed with a decision.
        user_response = interrupt(approval_payload)

        # Parse user response
        decision_data = user_response if isinstance(user_response, dict) else {"decision": str(user_response)}
        decision_obj = ApprovalDecision(
            decision=decision_data.get("decision", "reject"),
            reason=decision_data.get("reason"),
            modified_arguments=decision_data.get("modified_arguments"),
        )
        hitl_mgr.resolve_request(appr_req.id, decision_obj)

        logger.info(
            f"Resumed from HITL interrupt for {appr_req.id}: {decision_obj.decision}"
        )

        return {
            "pending_approval": approval_payload,
            "approval_decision": decision_data,
        }

    return approval_node
