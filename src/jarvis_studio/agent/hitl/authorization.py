"""Unified execution authorization validator for tool calls."""

from typing import Any

from pydantic import BaseModel

from jarvis_studio.agent.hitl.manager import (
    ApprovalStatus,
    HITLManager,
    compute_arguments_hash,
    get_hitl_manager,
)
from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


class AuthorizationResult(BaseModel):
    """Result of validating execution authorization for a specific tool call."""

    is_authorized: bool
    effective_args: dict[str, Any]
    rejection_reason: str | None = None
    status: str  # authorized_safe, authorized_approved, authorized_executing, authorized_modified, unauthorized_missing_thread, unauthorized_unapproved, unauthorized_rejected, unauthorized_mismatch, unauthorized_invalid_args, unauthorized_already_consumed, unauthorized_lease_failed
    approval_id: str | None = None


def extract_valid_thread_id(state: AgentState | dict[str, Any]) -> str | None:
    """Extract and validate a real thread_id from execution state.

    Shared authoritative identity rule used by both approval_gate and executor:
    - Requires a dict `run_context` containing a non-empty string `session_id`.
    - Explicitly rejects the reserved placeholder `'default'`.
    """
    run_context = state.get("run_context") if isinstance(state, dict) else None
    if not isinstance(run_context, dict):
        return None
    raw_tid = run_context.get("session_id")
    if not isinstance(raw_tid, str):
        return None
    cleaned = raw_tid.strip()
    if not cleaned or cleaned.lower() == "default":
        return None
    return cleaned


def verify_tool_execution_authorization(
    tool_name: str,
    tool_call_id: str,
    raw_args: dict[str, Any],
    state: AgentState | dict[str, Any],
    registry: ToolRegistry | None = None,
    hitl_mgr: HITLManager | None = None,
    acquire_lease: bool = True,
) -> AuthorizationResult:
    """Rigorous gatekeeper validating that a tool call is authorized to execute.

    Checks:
    1. If the tool is SAFE / does not require approval -> authorized.
    2. If the tool is HIGH RISK / requires approval:
       - Must have valid `thread_id` in `state["run_context"]["session_id"]` (Fail-Closed).
       - Must have an explicit approval decision bound to `thread_id` and `tool_call_id`.
       - Cannot use decisions targeted at other threads or other tool calls.
       - Original arguments must match arguments recorded during approval (tamper check).
       - If rejected -> unauthorized with rejection reason.
       - If modified -> arguments validated against tool schema; unauthorized if schema invalid.
       - If approved -> atomically acquire execution lease (status -> EXECUTING).
       - If already consumed, failed, unapproved, or missing -> strictly blocked from execution.
    """
    tool_registry = registry or get_default_registry()

    # 1. Check if tool requires approval
    if not tool_registry.requires_approval(tool_name):
        return AuthorizationResult(
            is_authorized=True,
            effective_args=raw_args,
            status="authorized_safe",
        )

    # 2. Extract and validate thread_id (Strict Thread Isolation)
    thread_id = extract_valid_thread_id(state)
    if not thread_id:
        logger.warning(
            f"Execution blocked: Missing thread_id in context for dangerous tool '{tool_name}' (call_id: {tool_call_id})"
        )
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_missing_thread",
            rejection_reason="Execution blocked: Missing required thread_id in execution context.",
        )

    # 3. Tool requires approval - lookup decision
    hitl = hitl_mgr or get_hitl_manager()
    approval_decision = state.get("approval_decision") or {}

    decision_data: dict[str, Any] | None = None

    # Check decisions_by_call_id map first
    decisions_by_call = approval_decision.get("decisions_by_call_id")
    if isinstance(decisions_by_call, dict) and tool_call_id in decisions_by_call:
        decision_data = decisions_by_call[tool_call_id]

    # Check top-level if matches this tool_call_id
    if not decision_data and approval_decision.get("tool_call_id"):
        if approval_decision.get("tool_call_id") == tool_call_id:
            decision_data = approval_decision
        else:
            logger.warning(
                f"Authorization mismatch: Decision for call_id '{approval_decision.get('tool_call_id')}' "
                f"does not match requested call_id '{tool_call_id}'"
            )
            return AuthorizationResult(
                is_authorized=False,
                effective_args=raw_args,
                status="unauthorized_mismatch",
                rejection_reason=(
                    f"Authorization mismatch: Tool call ID '{tool_call_id}' "
                    f"does not match approval grant for '{approval_decision.get('tool_call_id')}'."
                ),
            )

    # Check if a single generic decision was provided (legacy compatibility)
    if not decision_data and approval_decision.get("decision"):
        target_appr_id = approval_decision.get("approval_id")
        if target_appr_id:
            req = hitl.get_request(target_appr_id, thread_id=thread_id)
            if not req or (req.tool_call_id and req.tool_call_id != tool_call_id):
                return AuthorizationResult(
                    is_authorized=False,
                    effective_args=raw_args,
                    status="unauthorized_mismatch",
                    rejection_reason="Authorization mismatch: Approval request belongs to another thread or tool call.",
                    approval_id=target_appr_id,
                )
        decision_data = approval_decision

    # Check hitl_mgr as fallback if decision was stored in manager
    if not decision_data:
        req = hitl.get_by_tool_call_id(tool_call_id, thread_id=thread_id)
        if req:
            if req.status in (ApprovalStatus.CONSUMED, ApprovalStatus.FAILED):
                return AuthorizationResult(
                    is_authorized=False,
                    effective_args=raw_args,
                    status="unauthorized_already_consumed",
                    rejection_reason=f"Execution blocked: Approval request '{req.id}' is already {req.status.value}.",
                    approval_id=req.id,
                )
            if req.status != ApprovalStatus.PENDING:
                decision_data = {
                    "decision": req.decision or req.status.value.lower(),
                    "reason": req.resolution_reason,
                    "modified_arguments": req.modified_arguments,
                    "arguments_hash": req.arguments_hash,
                    "approval_id": req.id,
                    "tool_call_id": req.tool_call_id,
                }

    if not decision_data or not decision_data.get("decision"):
        logger.warning(
            f"Execution blocked: Dangerous tool '{tool_name}' (call_id: {tool_call_id}) has no valid approval"
        )
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_unapproved",
            rejection_reason=f"Execution blocked: High-risk tool '{tool_name}' (call_id: {tool_call_id}) has not received valid approval.",
        )

    # 4. Verify arguments hash (Tamper Detection)
    expected_hash = decision_data.get("arguments_hash")
    raw_appr_id = decision_data.get("approval_id")
    appr_id = (
        raw_appr_id.strip()
        if isinstance(raw_appr_id, str) and raw_appr_id.strip()
        else None
    )
    bound_req = hitl.get_request(appr_id, thread_id=thread_id) if appr_id else None
    if not expected_hash:
        if bound_req:
            expected_hash = bound_req.arguments_hash
        elif raw_appr_id is None:
            req = hitl.get_by_tool_call_id(tool_call_id, thread_id=thread_id)
            if req:
                expected_hash = req.arguments_hash

    if not expected_hash:
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_unapproved",
            rejection_reason=f"Execution blocked: No valid arguments hash found for tool '{tool_name}' (call_id: {tool_call_id}).",
            approval_id=appr_id,
        )

    actual_hash = compute_arguments_hash(raw_args)
    if actual_hash != expected_hash:
        logger.warning(
            f"Authorization mismatch for tool '{tool_name}': actual hash '{actual_hash}' != expected '{expected_hash}'"
        )
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_mismatch",
            rejection_reason=(
                f"Authorization mismatch: Original tool arguments do not match approved request. "
                f"(expected: {expected_hash}, current: {actual_hash})"
            ),
            approval_id=appr_id,
        )

    decision_type = str(decision_data.get("decision", "")).lower()
    decision_reason = decision_data.get("reason", "No reason provided")

    # 5. Handle rejection
    if decision_type == "reject":
        logger.info(
            f"Tool '{tool_name}' (call_id: {tool_call_id}) rejected: {decision_reason}"
        )
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_rejected",
            rejection_reason=f"User rejected execution of '{tool_name}'. Reason: {decision_reason}",
            approval_id=appr_id,
        )

    # 6. Handle argument modification
    effective_args = raw_args
    if decision_type == "modify":
        modified_args = decision_data.get("modified_arguments")
        if not isinstance(modified_args, dict):
            return AuthorizationResult(
                is_authorized=False,
                effective_args=raw_args,
                status="unauthorized_invalid_args",
                rejection_reason="Modified arguments must be provided as a dictionary.",
                approval_id=appr_id,
            )

        if bound_req is not None:
            if bound_req.status != ApprovalStatus.MODIFIED:
                return AuthorizationResult(
                    is_authorized=False,
                    effective_args=raw_args,
                    status="unauthorized_mismatch",
                    rejection_reason=(
                        f"Authorization mismatch: Approval '{appr_id}' has status "
                        f"'{bound_req.status.value}', cannot apply modified arguments."
                    ),
                    approval_id=appr_id,
                )
            if bound_req.modified_arguments is not None and compute_arguments_hash(
                modified_args
            ) != compute_arguments_hash(bound_req.modified_arguments):
                return AuthorizationResult(
                    is_authorized=False,
                    effective_args=raw_args,
                    status="unauthorized_mismatch",
                    rejection_reason="Authorization mismatch: Modified arguments do not match approved request.",
                    approval_id=appr_id,
                )

        # Validate modified arguments against tool schema
        tool = tool_registry.get(tool_name)
        if tool and hasattr(tool, "args_schema") and tool.args_schema:
            try:
                tool.args_schema.model_validate(modified_args)
            except Exception as val_err:  # noqa: BLE001
                logger.error(
                    f"Modified arguments validation failed for '{tool_name}': {val_err}"
                )
                return AuthorizationResult(
                    is_authorized=False,
                    effective_args=raw_args,
                    status="unauthorized_invalid_args",
                    rejection_reason=f"Modified arguments failed schema validation: {val_err}",
                    approval_id=appr_id,
                )

        effective_args = modified_args

    if decision_type not in ("approve", "modify"):
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_unapproved",
            rejection_reason=f"Invalid or unrecognized approval decision '{decision_type}'.",
            approval_id=appr_id,
        )

    if (
        decision_type == "approve"
        and bound_req is not None
        and bound_req.status == ApprovalStatus.MODIFIED
    ):
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_mismatch",
            rejection_reason=(
                f"Authorization mismatch: Approval '{appr_id}' was resolved as MODIFIED, cannot execute raw approve."
            ),
            approval_id=appr_id,
        )

    # 7. Atomic Execution Lease Acquisition (One-Time Execution Protection)
    if acquire_lease:
        if not appr_id:
            logger.warning(
                f"Execution blocked: Missing required approval_id for tool '{tool_name}' (call_id: {tool_call_id})"
            )
            return AuthorizationResult(
                is_authorized=False,
                effective_args=raw_args,
                status="unauthorized_unapproved",
                rejection_reason=(
                    f"Execution blocked: Missing required approval_id to acquire execution lease "
                    f"for tool '{tool_name}' (call_id: {tool_call_id})."
                ),
            )

        success, _leased_req, err = hitl.acquire_execution_lease(
            approval_id=appr_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            arguments_hash=expected_hash,
        )
        if not success:
            logger.warning(
                f"Execution lease acquisition failed for tool '{tool_name}' (ID: {appr_id}): {err}"
            )
            return AuthorizationResult(
                is_authorized=False,
                effective_args=raw_args,
                status="unauthorized_lease_failed",
                rejection_reason=f"Execution lease acquisition failed: {err}",
                approval_id=appr_id,
            )
    elif appr_id and not bound_req:
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_mismatch",
            rejection_reason=(
                f"Authorization mismatch: Approval request '{appr_id}' does not belong to thread '{thread_id}'."
            ),
            approval_id=appr_id,
        )

    logger.info(
        f"Tool '{tool_name}' (call_id: {tool_call_id}, approval_id: {appr_id}) authorized for execution."
    )
    return AuthorizationResult(
        is_authorized=True,
        effective_args=effective_args,
        status="authorized_executing" if acquire_lease else "authorized_approved",
        approval_id=appr_id,
    )
