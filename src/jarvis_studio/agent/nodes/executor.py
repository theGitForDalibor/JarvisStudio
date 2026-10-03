"""Tool execution node handling normal and HITL-resolved tool calls."""

import json
from typing import Any, Dict, List
from langchain_core.messages import AIMessage, ToolMessage

from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.tracer import timed_step
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def create_tool_executor_node(registry: ToolRegistry = None):
    """Factory creating the tool execution node."""
    tool_registry = registry or get_default_registry()

    async def tool_executor_node(state: AgentState) -> Dict[str, Any]:
        """Execute pending tool calls in the latest AIMessage."""
        messages = state.get("messages", [])
        if not messages:
            return {}

        last_message = messages[-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        approval_decision = state.get("approval_decision") or {}
        decision_type = approval_decision.get("decision", "").lower()
        decision_reason = approval_decision.get("reason", "No reason provided")
        modified_args = approval_decision.get("modified_arguments")

        tool_messages: List[ToolMessage] = []

        for call in last_message.tool_calls:
            tool_name = call.get("name", "")
            tool_args = call.get("args", {})
            call_id = call.get("id", "")

            # Check if this specific tool was rejected
            if tool_registry.requires_approval(tool_name) and decision_type == "reject":
                logger.info(f"Skipping tool '{tool_name}' due to user rejection.")
                tool_messages.append(
                    ToolMessage(
                        content=f"Operation cancelled: User rejected execution of '{tool_name}'. Reason: {decision_reason}",
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

            # Check if arguments were modified
            effective_args = tool_args
            if tool_registry.requires_approval(tool_name) and decision_type == "modify" and modified_args:
                logger.info(f"Applying modified arguments for tool '{tool_name}': {modified_args}")
                effective_args = modified_args

            # Lookup tool
            tool = tool_registry.get(tool_name)
            if not tool:
                tool_messages.append(
                    ToolMessage(
                        content=f"Error: Tool '{tool_name}' is not recognized.",
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

            with timed_step(f"tool_exec_{tool_name}", tool_name=tool_name):
                try:
                    if hasattr(tool, "ainvoke"):
                        result = await tool.ainvoke(effective_args)
                    else:
                        result = tool.invoke(effective_args)
                    result_str = str(result)
                except Exception as exc:
                    logger.error(f"Error executing tool '{tool_name}': {exc}")
                    result_str = f"Error executing '{tool_name}': {exc}"

            tool_messages.append(
                ToolMessage(
                    content=result_str,
                    tool_call_id=call_id,
                    name=tool_name,
                )
            )

        return {
            "messages": tool_messages,
            "pending_approval": None,
            "approval_decision": None,
        }

    return tool_executor_node
