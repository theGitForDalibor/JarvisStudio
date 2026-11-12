"""Shared HITL approval and tool execution outcome models."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from jarvis_studio.tools.base import RiskLevel


def compute_arguments_hash(args: dict[str, Any] | None) -> str:
    """Compute deterministic SHA-256 hash of tool call arguments."""
    if not args:
        payload = "{}"
    else:
        try:
            payload = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = str(sorted(args.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ApprovalStatus(str, Enum):
    """Status lifecycle of an approval request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFIED = "MODIFIED"
    EXECUTING = "EXECUTING"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ExecutionOutcomeStatus(str, Enum):
    """Objective outcome status of a physical tool execution."""

    NOT_STARTED = "NOT_STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class ApprovalRequest(BaseModel):
    """Record of an operation requiring human confirmation."""

    id: str = Field(default_factory=lambda: f"appr_{uuid.uuid4().hex[:8]}")
    thread_id: str
    run_id: str
    task_id: str | None = None
    checkpoint_id: str | None = None
    tool_name: str
    arguments: dict[str, Any]
    arguments_hash: str = ""
    tool_call_id: str = ""
    risk_level: RiskLevel = RiskLevel.HIGH
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: str | None = None
    description: str = "High-risk tool call intercepted"
    created_at: float = Field(default_factory=time.time)
    resolved_at: float | None = None
    executed_at: float | None = None
    completed_at: float | None = None
    resolution_reason: str | None = None
    execution_error: str | None = None
    modified_arguments: dict[str, Any] | None = None
    lease_owner_instance_id: str | None = None
    lease_acquired_run_id: str | None = None
    audit_note: str | None = None
    audit_actor: str | None = None
    audited_at: float | None = None


class ApprovalDecision(BaseModel):
    """User decision payload to resolve a pending approval."""

    decision: str = Field(
        description="'approve', 'reject', or 'modify'",
    )
    reason: str | None = None
    modified_arguments: dict[str, Any] | None = None


class ExecutionOutcome(BaseModel):
    """Persistent execution outcome record for HIGH-risk and tracked SAFE tool calls."""

    outcome_id: str = Field(default_factory=lambda: f"out_{uuid.uuid4().hex[:12]}")
    thread_id: str
    task_id: str | None = None
    run_id: str
    tool_call_id: str
    approval_id: str | None = None
    tool_name: str
    is_high_risk: bool = False
    outcome_status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.NOT_STARTED
    side_effect_hint: dict[str, Any] | None = None
    result_summary: str | None = None
    error_message: str | None = None
    reconciled_by: str | None = None
    reconciled_note: str | None = None
    reconciled_at: float | None = None
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    updated_at: float = Field(default_factory=time.time)
