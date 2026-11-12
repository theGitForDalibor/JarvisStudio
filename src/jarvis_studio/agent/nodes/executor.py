"""Tool execution node handling normal and HITL-resolved tool calls."""

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from jarvis_studio.agent.hitl.authorization import (
    extract_valid_thread_id,
    verify_tool_execution_authorization,
)
from jarvis_studio.agent.hitl.manager import (
    ApprovalStatus,
    DuplicateToolCallError,
    ExecutionOutcomeStatus,
    HITLManager,
    get_hitl_manager,
)
from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.tracer import timed_step
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def create_tool_executor_node(
    registry: ToolRegistry = None,
    *,
    hitl_mgr: HITLManager | None = None,
):
    """Factory creating the tool execution node."""
    tool_registry = registry or get_default_registry()
    bound_hitl_mgr = hitl_mgr

    async def tool_executor_node(state: AgentState) -> dict[str, Any]:
        """Execute pending tool calls in the latest AIMessage with strict authorization checks."""
        messages = state.get("messages", [])
        if not messages:
            return {}

        last_message = messages[-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        tool_messages: list[ToolMessage] = []
        active_hitl = bound_hitl_mgr or get_hitl_manager()
        thread_id = extract_valid_thread_id(state)
        run_ctx = state.get("run_context") or {}
        raw_run_id = run_ctx.get("run_id") if isinstance(run_ctx, dict) else None
        run_id = (
            raw_run_id.strip()
            if isinstance(raw_run_id, str) and raw_run_id.strip()
            else "run_unknown"
        )

        for call in last_message.tool_calls:
            tool_name = call.get("name", "")
            tool_args = call.get("args", {})
            call_id = call.get("id", "")

            # Mandatory Authorization & Lease Acquisition Check before execution
            auth = await asyncio.to_thread(
                verify_tool_execution_authorization,
                tool_name=tool_name,
                tool_call_id=call_id,
                raw_args=tool_args,
                state=state,
                registry=tool_registry,
                hitl_mgr=active_hitl,
                acquire_lease=True,
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

            tracked_safe = bool(
                auth.approval_id is None
                and thread_id is not None
                and isinstance(call_id, str)
                and call_id.strip()
            )
            if tracked_safe:
                try:
                    await asyncio.to_thread(
                        active_hitl.register_safe_outcome_start,
                        thread_id=thread_id,  # type: ignore[arg-type]
                        run_id=run_id,
                        tool_call_id=call_id,
                        tool_name=tool_name,
                    )
                except DuplicateToolCallError as dup_exc:
                    logger.warning(
                        f"Blocked duplicate SAFE tool call '{tool_name}' (call_id={call_id}): {dup_exc}"
                    )
                    tool_messages.append(
                        ToolMessage(
                            content=f"Operation cancelled: {dup_exc}",
                            tool_call_id=call_id,
                            name=tool_name,
                        )
                    )
                    continue
                except Exception as reg_exc:
                    logger.error(
                        f"Fail-Closed: could not persist pre-execution outcome for SAFE tool '{tool_name}' "
                        f"(call_id={call_id}): {reg_exc}"
                    )
                    tool_messages.append(
                        ToolMessage(
                            content=(
                                f"Operation cancelled: Failed to persist pre-execution outcome "
                                f"for '{tool_name}': {reg_exc}"
                            ),
                            tool_call_id=call_id,
                            name=tool_name,
                        )
                    )
                    continue

            # Lookup tool
            tool = tool_registry.get(tool_name)
            if not tool:
                if auth.approval_id:
                    await asyncio.to_thread(
                        active_hitl.finalize_execution,
                        auth.approval_id,
                        success=False,
                        error=f"Tool '{tool_name}' is not recognized.",
                    )
                elif tracked_safe:
                    await asyncio.to_thread(
                        active_hitl.finalize_safe_outcome,
                        thread_id=thread_id,  # type: ignore[arg-type]
                        tool_call_id=call_id,
                        outcome_status=ExecutionOutcomeStatus.FAILED,
                        error_message=f"Tool '{tool_name}' is not recognized.",
                    )
                tool_messages.append(
                    ToolMessage(
                        content=f"Error: Tool '{tool_name}' is not recognized.",
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

            effective_args = auth.effective_args
            tool_succeeded = False
            execution_output = ""

            with timed_step(f"tool_exec_{tool_name}", tool_name=tool_name):
                try:
                    if hasattr(tool, "ainvoke"):
                        result = await tool.ainvoke(effective_args)
                    else:
                        result = tool.invoke(effective_args)
                    execution_output = str(result)
                    tool_succeeded = True
                except asyncio.CancelledError:
                    logger.warning(
                        f"Tool execution cancelled via CancelledError: '{tool_name}' "
                        f"(call_id={call_id}, approval_id={auth.approval_id})"
                    )
                    if auth.approval_id:
                        try:
                            active_hitl.finalize_execution(
                                auth.approval_id,
                                success=False,
                                error="Task execution cancelled (asyncio.CancelledError)",
                            )
                        except Exception as fin_exc:
                            logger.error(
                                f"Failed to finalize cancelled execution for approval '{auth.approval_id}': {fin_exc}"
                            )
                        try:
                            active_hitl.mark_outcome_uncertain(
                                approval_id=auth.approval_id,
                                error="Task execution cancelled in flight (asyncio.CancelledError)",
                            )
                        except Exception as unk_exc:
                            logger.error(
                                f"Failed to mark uncertain outcome for approval '{auth.approval_id}': {unk_exc}"
                            )
                    elif tracked_safe:
                        try:
                            active_hitl.finalize_safe_outcome(
                                thread_id=thread_id,  # type: ignore[arg-type]
                                tool_call_id=call_id,
                                outcome_status=ExecutionOutcomeStatus.UNKNOWN,
                                error_message="Task execution cancelled in flight (asyncio.CancelledError)",
                            )
                        except Exception as unk_exc:
                            logger.error(
                                f"Failed to mark uncertain outcome for SAFE call '{call_id}': {unk_exc}"
                            )
                    raise
                except Exception as exc:
                    logger.error(f"Error executing tool '{tool_name}': {exc}")
                    execution_output = f"Error executing '{tool_name}': {exc}"
                    if auth.approval_id:
                        try:
                            await asyncio.to_thread(
                                active_hitl.finalize_execution,
                                auth.approval_id,
                                success=False,
                                error=str(exc),
                            )
                        except Exception as fin_exc:
                            logger.error(
                                f"Failed to finalize failed execution for approval '{auth.approval_id}': {fin_exc}"
                            )
                    elif tracked_safe:
                        try:
                            await asyncio.to_thread(
                                active_hitl.finalize_safe_outcome,
                                thread_id=thread_id,  # type: ignore[arg-type]
                                tool_call_id=call_id,
                                outcome_status=ExecutionOutcomeStatus.FAILED,
                                error_message=str(exc),
                            )
                        except Exception as fin_exc:
                            logger.error(
                                f"Failed to finalize failed SAFE outcome for '{call_id}': {fin_exc}"
                            )

            msg_kwargs: dict[str, Any] = {}
            if tool_succeeded:
                result_str = execution_output
                if auth.approval_id:
                    try:
                        finalized_req = await asyncio.to_thread(
                            active_hitl.finalize_execution,
                            auth.approval_id,
                            success=True,
                        )
                        if (
                            finalized_req is None
                            or finalized_req.status != ApprovalStatus.CONSUMED
                        ):
                            actual_status = (
                                finalized_req.status.value
                                if finalized_req is not None
                                else "MISSING"
                            )
                            raise RuntimeError(
                                f"Approval '{auth.approval_id}' did not transition to CONSUMED (status={actual_status})"
                            )
                    except Exception as fin_exc:
                        logger.error(
                            f"CRITICAL: Tool '{tool_name}' (call_id={call_id}, approval_id={auth.approval_id}) "
                            f"executed successfully, but authorization finalization failed: {fin_exc}"
                        )
                        result_str = (
                            f"{execution_output}\n"
                            f"[AUTHORIZATION_FINALIZATION_ERROR] Tool '{tool_name}' executed successfully, "
                            f"but updating approval state '{auth.approval_id}' failed ({fin_exc}). "
                            f"Re-execution remains blocked."
                        )
                        msg_kwargs = {
                            "tool_succeeded": True,
                            "authorization_finalization_error": str(fin_exc),
                            "approval_id": auth.approval_id,
                        }
                elif tracked_safe:
                    try:
                        await asyncio.to_thread(
                            active_hitl.finalize_safe_outcome,
                            thread_id=thread_id,  # type: ignore[arg-type]
                            tool_call_id=call_id,
                            outcome_status=ExecutionOutcomeStatus.SUCCEEDED,
                            result_summary=execution_output[:500],
                        )
                    except Exception as out_exc:
                        logger.error(
                            f"CRITICAL: SAFE tool '{tool_name}' (call_id={call_id}) succeeded, "
                            f"but outcome finalization failed: {out_exc}"
                        )
                        result_str = (
                            f"{execution_output}\n"
                            f"[OUTCOME_FINALIZATION_ERROR] Tool '{tool_name}' executed successfully, "
                            f"but updating outcome record failed ({out_exc}). "
                            f"Re-execution remains blocked."
                        )
                        msg_kwargs = {
                            "tool_succeeded": True,
                            "outcome_finalization_error": str(out_exc),
                        }
            else:
                result_str = execution_output

            tool_messages.append(
                ToolMessage(
                    content=result_str,
                    tool_call_id=call_id,
                    name=tool_name,
                    additional_kwargs=msg_kwargs,
                )
            )

        return {
            "messages": tool_messages,
            "pending_approval": None,
            "approval_decision": None,
        }

    return tool_executor_node
