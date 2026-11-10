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
    status: str  # authorized_safe, authorized_approved, authorized_modified, unauthorized_unapproved, unauthorized_rejected, unauthorized_mismatch, unauthorized_invalid_args
    approval_id: str | None = None


def verify_tool_execution_authorization(
    tool_name: str,
    tool_call_id: str,
    raw_args: dict[str, Any],
    state: AgentState | dict[str, Any],
    registry: ToolRegistry | None = None,
    hitl_mgr: HITLManager | None = None,
) -> AuthorizationResult:
    """Rigorous gatekeeper validating that a tool call is authorized to execute.

    Checks:
    1. If the tool is SAFE / does not require approval -> authorized.
    2. If the tool is HIGH RISK / requires approval:
       - Must have an explicit approval decision bound to this `tool_call_id`.
       - Cannot use decisions targeted at other tool calls.
       - Original arguments must match arguments recorded during approval (tamper check).
       - If rejected -> unauthorized with rejection reason.
       - If modified -> arguments validated against tool schema; unauthorized if schema invalid.
       - If approved -> authorized with original arguments.
       - If unapproved or missing -> strictly blocked from execution.
    """
    tool_registry = registry or get_default_registry()

    # 1. Check if tool requires approval
    if not tool_registry.requires_approval(tool_name):
        return AuthorizationResult(
            is_authorized=True,
            effective_args=raw_args,
            status="authorized_safe",
        )

    # 2. Tool requires approval - lookup decision
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
        # If there's an explicit tool_call_id in approval_decision that differed, we already caught it above.
        # If tool_call_id was omitted in approval_decision, check if it's the only call or matched in hitl_mgr
        target_appr_id = approval_decision.get("approval_id")
        if target_appr_id:
            req = hitl.get_request(target_appr_id)
            if req and req.tool_call_id and req.tool_call_id != tool_call_id:
                return AuthorizationResult(
                    is_authorized=False,
                    effective_args=raw_args,
                    status="unauthorized_mismatch",
                    rejection_reason="Authorization mismatch: Approval request belongs to another tool call.",
                    approval_id=target_appr_id,
                )
        decision_data = approval_decision

    # Check hitl_mgr as fallback if decision was stored in manager
    if not decision_data:
        run_context = state.get("run_context", {})
        thread_id = run_context.get("session_id")
        req = hitl.get_by_tool_call_id(tool_call_id, thread_id)
        if req and req.status != ApprovalStatus.PENDING:
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

    # 3. Verify arguments hash (Tamper Detection)
    expected_hash = decision_data.get("arguments_hash")
    if not expected_hash:
        appr_id = decision_data.get("approval_id")
        if appr_id:
            req = hitl.get_request(appr_id)
            if req:
                expected_hash = req.arguments_hash
        if not expected_hash:
            req = hitl.get_by_tool_call_id(tool_call_id)
            if req:
                expected_hash = req.arguments_hash

    actual_hash = compute_arguments_hash(raw_args)
    if expected_hash and actual_hash != expected_hash:
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
            approval_id=decision_data.get("approval_id"),
        )

    decision_type = str(decision_data.get("decision", "")).lower()
    decision_reason = decision_data.get("reason", "No reason provided")

    # 4. Handle rejection
    if decision_type == "reject":
        logger.info(
            f"Tool '{tool_name}' (call_id: {tool_call_id}) rejected: {decision_reason}"
        )
        return AuthorizationResult(
            is_authorized=False,
            effective_args=raw_args,
            status="unauthorized_rejected",
            rejection_reason=f"User rejected execution of '{tool_name}'. Reason: {decision_reason}",
            approval_id=decision_data.get("approval_id"),
        )

    # 5. Handle argument modification
    if decision_type == "modify":
        modified_args = decision_data.get("modified_arguments")
        if not isinstance(modified_args, dict):
            return AuthorizationResult(
                is_authorized=False,
                effective_args=raw_args,
                status="unauthorized_invalid_args",
                rejection_reason="Modified arguments must be provided as a dictionary.",
                approval_id=decision_data.get("approval_id"),
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
                    approval_id=decision_data.get("approval_id"),
                )

        logger.info(
            f"Tool '{tool_name}' (call_id: {tool_call_id}) authorized with modified arguments."
        )
        return AuthorizationResult(
            is_authorized=True,
            effective_args=modified_args,
            status="authorized_modified",
            approval_id=decision_data.get("approval_id"),
        )

    # 6. Handle approval
    if decision_type == "approve":
        logger.info(
            f"Tool '{tool_name}' (call_id: {tool_call_id}) authorized by user approval."
        )
        return AuthorizationResult(
            is_authorized=True,
            effective_args=raw_args,
            status="authorized_approved",
            approval_id=decision_data.get("approval_id"),
        )

    return AuthorizationResult(
        is_authorized=False,
        effective_args=raw_args,
        status="unauthorized_unapproved",
        rejection_reason=f"Invalid or unrecognized approval decision '{decision_type}'.",
        approval_id=decision_data.get("approval_id"),
    )
