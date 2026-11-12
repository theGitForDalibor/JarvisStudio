"""Approval gate node leveraging LangGraph interrupt for HITL."""

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from jarvis_studio.agent.hitl.authorization import extract_valid_thread_id
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    HITLManager,
    get_hitl_manager,
)
from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def _resolve_single_item(
    item: dict[str, Any],
    all_requests: list[ApprovalRequest],
    hitl_mgr: Any,
    thread_id: str,
) -> None:
    """Resolve a single approval request from decision data."""
    appr_id = item.get("approval_id")
    tool_call_id = item.get("tool_call_id")

    target_req: ApprovalRequest | None = None
    if appr_id:
        target_req = hitl_mgr.get_request(appr_id, thread_id=thread_id)
    elif tool_call_id:
        for r in all_requests:
            if r.tool_call_id == tool_call_id and r.thread_id == thread_id:
                target_req = r
                break

    if not target_req:
        logger.warning(f"No matching approval request found for item: {item}")
        return

    # If already resolved in a prior interrupt cycle, skip
    if target_req.status != ApprovalStatus.PENDING:
        return

    dec_obj = ApprovalDecision(
        decision=item.get("decision", "reject"),
        reason=item.get("reason"),
        modified_arguments=item.get("modified_arguments"),
    )
    try:
        hitl_mgr.resolve_request(target_req.id, dec_obj, thread_id=thread_id)
    except (ValueError, PermissionError) as exc:
        logger.warning(f"Error resolving request {target_req.id}: {exc}")


def _apply_user_response(
    user_response: Any,
    pending: list[ApprovalRequest],
    all_requests: list[ApprovalRequest],
    hitl_mgr: Any,
    thread_id: str,
) -> None:
    """Apply the response from interrupt to pending approval requests."""
    if not isinstance(user_response, dict):
        user_response = {"decision": str(user_response)}

    # 1. Batch decisions: list format
    batch = user_response.get("decisions")
    if isinstance(batch, list):
        for item in batch:
            if isinstance(item, dict):
                _resolve_single_item(item, all_requests, hitl_mgr, thread_id)
        return

    # 2. Batch decisions: dict format {call_id: decision}
    if isinstance(batch, dict):
        for key, val in batch.items():
            if isinstance(val, dict):
                item = dict(val)
                if "tool_call_id" not in item and "approval_id" not in item:
                    if key.startswith("appr_"):
                        item["approval_id"] = key
                    else:
                        item["tool_call_id"] = key
                _resolve_single_item(item, all_requests, hitl_mgr, thread_id)
        return

    # 3. Targeted single decision
    if user_response.get("tool_call_id") or user_response.get("approval_id"):
        _resolve_single_item(user_response, all_requests, hitl_mgr, thread_id)
        return

    # 4. Generic single decision: resolve the current primary pending request
    if pending:
        primary = pending[0]
        if primary.status != ApprovalStatus.PENDING:
            return
        dec_obj = ApprovalDecision(
            decision=user_response.get("decision", "reject"),
            reason=user_response.get("reason"),
            modified_arguments=user_response.get("modified_arguments"),
        )
        try:
            hitl_mgr.resolve_request(primary.id, dec_obj, thread_id=thread_id)
        except (ValueError, PermissionError) as exc:
            logger.warning(f"Error resolving approval request {primary.id}: {exc}")


def create_approval_node(
    registry: ToolRegistry = None,
    *,
    hitl_mgr: HITLManager | None = None,
):
    """Create the approval gate node checking high-risk tool calls."""
    tool_registry = registry or get_default_registry()
    bound_hitl_mgr = hitl_mgr

    def approval_node(state: AgentState) -> dict[str, Any]:
        """Intercept high-risk tool calls and trigger LangGraph interrupt."""
        messages = state.get("messages", [])
        if not messages:
            return {}

        last_message = messages[-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        # Find dangerous tool calls requiring confirmation
        dangerous_calls = [
            call
            for call in last_message.tool_calls
            if tool_registry.requires_approval(call.get("name", ""))
        ]

        if not dangerous_calls:
            return {}

        active_hitl = bound_hitl_mgr or get_hitl_manager()
        # Strict Fail-Closed thread identity validation aligned with authorization.py
        thread_id = extract_valid_thread_id(state)
        if not thread_id:
            logger.warning(
                "HITL approval blocked (Fail-Closed): Missing or invalid thread_id ('default'/empty) in run_context."
            )
            return {
                "pending_approval": None,
                "approval_decision": None,
            }

        run_context = state.get("run_context") or {}
        run_id = (
            run_context.get("run_id", "run_unknown")
            if isinstance(run_context, dict)
            else "run_unknown"
        )

        # Record approval requests for all dangerous calls
        all_requests: list[ApprovalRequest] = []
        for call in dangerous_calls:
            tool_name = call.get("name", "unknown")
            args = call.get("args", {})
            tool_call_id = call.get("id", "")

            existing_req = active_hitl.get_by_tool_call_id(tool_call_id, thread_id)
            if existing_req:
                all_requests.append(existing_req)
            else:
                req = active_hitl.create_request(
                    thread_id=thread_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    arguments=args,
                    tool_call_id=tool_call_id,
                    description=f"Action '{tool_name}' requires confirmation",
                )
                all_requests.append(req)

        # Loop until all dangerous calls have been resolved
        while True:
            pending = [r for r in all_requests if r.status == ApprovalStatus.PENDING]
            if not pending:
                break

            primary = pending[0]
            approval_payload = {
                "approval_id": primary.id,
                "thread_id": thread_id,
                "run_id": run_id,
                "tool_call_id": primary.tool_call_id,
                "tool_name": primary.tool_name,
                "arguments": primary.arguments,
                "arguments_hash": primary.arguments_hash,
                "risk_level": primary.risk_level.value,
                "description": primary.description,
                "pending_count": len(pending),
                "requests": [
                    {
                        "approval_id": r.id,
                        "thread_id": r.thread_id,
                        "tool_call_id": r.tool_call_id,
                        "tool_name": r.tool_name,
                        "arguments": r.arguments,
                        "arguments_hash": r.arguments_hash,
                        "risk_level": r.risk_level.value,
                        "status": r.status.value,
                        "description": r.description,
                    }
                    for r in all_requests
                ],
            }

            logger.warning(
                f"Pausing execution for HITL approval: {primary.tool_name} "
                f"(ID: {primary.id}, call_id: {primary.tool_call_id}, remaining pending: {len(pending)})"
            )

            # Trigger native LangGraph interrupt. Execution pauses until resumed with a decision.
            user_response = interrupt(approval_payload)

            # Parse and apply user response
            _apply_user_response(
                user_response, pending, all_requests, active_hitl, thread_id
            )

        # Construct decision mapping for executor
        decisions_by_call_id: dict[str, dict[str, Any]] = {}
        for r in all_requests:
            decisions_by_call_id[r.tool_call_id] = {
                "approval_id": r.id,
                "tool_call_id": r.tool_call_id,
                "tool_name": r.tool_name,
                "arguments_hash": r.arguments_hash,
                "decision": (
                    "reject"
                    if r.status in (ApprovalStatus.FAILED, ApprovalStatus.REJECTED, ApprovalStatus.UNKNOWN)
                    else (r.decision or r.status.value.lower())
                ),
                "reason": r.resolution_reason,
                "modified_arguments": r.modified_arguments,
            }

        primary_req = all_requests[0] if all_requests else None
        return {
            "pending_approval": None,
            "approval_decision": {
                "decisions_by_call_id": decisions_by_call_id,
                "decision": primary_req.decision if primary_req else None,
                "reason": primary_req.resolution_reason if primary_req else None,
                "modified_arguments": primary_req.modified_arguments
                if primary_req
                else None,
                "tool_call_id": primary_req.tool_call_id if primary_req else None,
                "approval_id": primary_req.id if primary_req else None,
            },
        }

    return approval_node
