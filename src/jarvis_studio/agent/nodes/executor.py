"""Tool execution node handling normal and HITL-resolved tool calls."""

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from jarvis_studio.agent.hitl.authorization import verify_tool_execution_authorization
from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.tracer import timed_step
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def create_tool_executor_node(registry: ToolRegistry = None):
    """Factory creating the tool execution node."""
    tool_registry = registry or get_default_registry()

    async def tool_executor_node(state: AgentState) -> dict[str, Any]:
        """Execute pending tool calls in the latest AIMessage with strict authorization checks."""
        messages = state.get("messages", [])
        if not messages:
            return {}

        last_message = messages[-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        tool_messages: list[ToolMessage] = []

        for call in last_message.tool_calls:
            tool_name = call.get("name", "")
            tool_args = call.get("args", {})
            call_id = call.get("id", "")

            # Mandatory Authorization Check before any execution
            auth = verify_tool_execution_authorization(
                tool_name=tool_name,
                tool_call_id=call_id,
                raw_args=tool_args,
                state=state,
                registry=tool_registry,
            )

            if not auth.is_authorized:
                logger.warning(
                    f"Tool execution blocked: '{tool_name}' (call_id: {call_id}). "
                    f"Reason: {auth.rejection_reason}"
                )
                tool_messages.append(
                    ToolMessage(
                        content=f"Operation cancelled: {auth.rejection_reason}",
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

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

            effective_args = auth.effective_args
            with timed_step(f"tool_exec_{tool_name}", tool_name=tool_name):
                try:
                    if hasattr(tool, "ainvoke"):
                        result = await tool.ainvoke(effective_args)
                    else:
                        result = tool.invoke(effective_args)
                    result_str = str(result)
                except Exception as exc:  # noqa: BLE001
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
