"""Tool execution node handling normal and HITL-resolved tool calls."""

import asyncio
from collections.abc import Callable
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


def classify_tool_execution_exception(exc: BaseException) -> ExecutionOutcomeStatus:
    """Classify tool execution outcome status based on exception semantics.

    Returns:
        - ExecutionOutcomeStatus.FAILED if it is definitively proven that NO side effect occurred.
        - ExecutionOutcomeStatus.PARTIAL if the tool execution contract indicates partial completion.
        - ExecutionOutcomeStatus.UNKNOWN if it is uncertain whether side effects occurred.
    """
    explicit_status = getattr(exc, "outcome_status", None)
    if isinstance(explicit_status, ExecutionOutcomeStatus):
        return explicit_status
    if getattr(exc, "is_partial", False) or getattr(exc, "has_partial_side_effects", False):
        return ExecutionOutcomeStatus.PARTIAL
    if getattr(exc, "has_side_effects", None) is True or getattr(exc, "side_effects_occurred", None) is True:
        return ExecutionOutcomeStatus.UNKNOWN
    if getattr(exc, "has_side_effects", None) is False or getattr(exc, "side_effects_occurred", None) is False:
        return ExecutionOutcomeStatus.FAILED

    from jarvis_studio.agent.hitl.models import (
        ToolPartialExecutionError,
        ToolPreExecutionError,
        ToolUncertainExecutionError,
    )
    from jarvis_studio.core.process.models import (
        ProcessIsolationError,
        ProcessLaunchError,
    )

    if isinstance(exc, ToolPreExecutionError):
        return ExecutionOutcomeStatus.FAILED
    if isinstance(exc, ToolPartialExecutionError):
        return ExecutionOutcomeStatus.PARTIAL
    if isinstance(exc, ToolUncertainExecutionError):
        return ExecutionOutcomeStatus.UNKNOWN
    if isinstance(exc, (ProcessLaunchError, ProcessIsolationError)):
        return ExecutionOutcomeStatus.FAILED

    # Uncertain: arbitrary exception during/after invocation may have caused side effects
    return ExecutionOutcomeStatus.UNKNOWN


async def _execute_cancellation_cleanup_shielded(
    cleanup_fn: Callable[[], None],
) -> None:
    """Execute cleanup off the event loop thread, immune to secondary task cancellations.

    If a secondary CancelledError is raised while waiting for the thread pool future,
    we ensure the worker thread completes before re-raising CancelledError.
    """
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, cleanup_fn)
    try:
        await asyncio.shield(fut)
    except asyncio.CancelledError:
        # A secondary cancellation occurred while the cleanup thread was executing.
        # Await the underlying future so worker thread SQLite writes finish cleanly
        # and connections are returned before propagating cancellation.
        await fut
        raise

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
                except Exception as reg_exc:  # noqa: BLE001
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

                    def _perform_cancellation_cleanup(
                        target_auth=auth,
                        target_call_id=call_id,
                        target_thread_id=thread_id,
                        target_tracked_safe=tracked_safe,
                        target_hitl=active_hitl,
                    ) -> None:
                        if target_auth.approval_id:
                            try:
                                target_hitl.finalize_execution(
                                    target_auth.approval_id,
                                    success=False,
                                    error="Task execution cancelled in flight (asyncio.CancelledError)",
                                    outcome_status=ExecutionOutcomeStatus.UNKNOWN,
                                )
                            except Exception as fin_exc:  # noqa: BLE001  # noqa: BLE001
                                logger.error(
                                    f"Failed to finalize cancelled execution for approval '{target_auth.approval_id}': {fin_exc}"
                                )
                                try:
                                    target_hitl.mark_outcome_uncertain(
                                        approval_id=target_auth.approval_id,
                                        error="Task execution cancelled in flight (asyncio.CancelledError)",
                                    )
                                except Exception as unk_exc:  # noqa: BLE001
                                    logger.error(
                                        f"Failed to mark uncertain outcome for approval '{target_auth.approval_id}': {unk_exc}"
                                    )
                        elif target_tracked_safe:
                            try:
                                target_hitl.finalize_safe_outcome(
                                    thread_id=target_thread_id,  # type: ignore[arg-type]
                                    tool_call_id=target_call_id,
                                    outcome_status=ExecutionOutcomeStatus.UNKNOWN,
                                    error_message="Task execution cancelled in flight (asyncio.CancelledError)",
                                )
                            except Exception as unk_exc:  # noqa: BLE001
                                logger.error(
                                    f"Failed to mark uncertain outcome for SAFE call '{target_call_id}': {unk_exc}"
                                )

                    await _execute_cancellation_cleanup_shielded(_perform_cancellation_cleanup)
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"Error executing tool '{tool_name}': {exc}")
                    execution_output = f"Error executing '{tool_name}': {exc}"
                    outcome_status = classify_tool_execution_exception(exc)
                    if auth.approval_id:
                        try:
                            await asyncio.to_thread(
                                active_hitl.finalize_execution,
                                auth.approval_id,
                                success=False,
                                error=str(exc),
                                outcome_status=outcome_status,
                            )
                        except Exception as fin_exc:  # noqa: BLE001
                            logger.error(
                                f"Failed to finalize failed execution for approval '{auth.approval_id}': {fin_exc}"
                            )
                    elif tracked_safe:
                        try:
                            await asyncio.to_thread(
                                active_hitl.finalize_safe_outcome,
                                thread_id=thread_id,  # type: ignore[arg-type]
                                tool_call_id=call_id,
                                outcome_status=outcome_status,
                                error_message=str(exc),
                            )
                        except Exception as fin_exc:  # noqa: BLE001
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
                    except Exception as fin_exc:  # noqa: BLE001
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
                    except Exception as out_exc:  # noqa: BLE001
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
