"""Human-in-the-Loop (HITL) approval manager and schemas."""

import hashlib
import json
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from jarvis_studio.core.observability.logger import logger
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
    """Status of an approval request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFIED = "MODIFIED"


class ApprovalRequest(BaseModel):
    """Record of an operation requiring human confirmation."""

    id: str = Field(default_factory=lambda: f"appr_{uuid.uuid4().hex[:8]}")
    thread_id: str
    run_id: str
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
    resolution_reason: str | None = None
    modified_arguments: dict[str, Any] | None = None


class ApprovalDecision(BaseModel):
    """User decision payload to resolve a pending approval."""

    decision: str = Field(
        description="'approve', 'reject', or 'modify'",
    )
    reason: str | None = None
    modified_arguments: dict[str, Any] | None = None


class HITLManager:
    """In-memory manager tracking active and historical approval requests."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def create_request(
        self,
        thread_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str = "",
        risk_level: RiskLevel = RiskLevel.HIGH,
        description: str = "",
    ) -> ApprovalRequest:
        """Create and record a new approval request."""
        args_hash = compute_arguments_hash(arguments)
        req = ApprovalRequest(
            thread_id=thread_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            arguments_hash=args_hash,
            tool_call_id=tool_call_id,
            risk_level=risk_level,
            description=description or f"Execution of dangerous tool '{tool_name}'",
        )
        self._requests[req.id] = req
        logger.warning(
            f"HITL Interrupt created: [{req.id}] (call_id: '{tool_call_id}') "
            f"for tool '{tool_name}' on thread '{thread_id}'"
        )
        return req

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        """Look up an approval request by its ID."""
        return self._requests.get(approval_id)

    def get_by_tool_call_id(
        self, tool_call_id: str, thread_id: str | None = None
    ) -> ApprovalRequest | None:
        """Find the latest approval request matching a tool_call_id."""
        for req in reversed(list(self._requests.values())):
            if req.tool_call_id == tool_call_id and (
                thread_id is None or req.thread_id == thread_id
            ):
                return req
        return None

    def list_pending(self, thread_id: str | None = None) -> list[ApprovalRequest]:
        """List all pending approval requests, optionally filtered by thread ID."""
        return [
            r
            for r in self._requests.values()
            if r.status == ApprovalStatus.PENDING
            and (thread_id is None or r.thread_id == thread_id)
        ]

    def resolve_request(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRequest | None:
        """Apply user decision to resolve a pending approval.

        Raises:
            ValueError: If the approval request has already been resolved (idempotency guard).
        """
        req = self._requests.get(approval_id)
        if not req:
            return None

        # Idempotency / duplicate submission protection
        if req.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"Approval request '{approval_id}' has already been resolved with status '{req.status.value}'"
            )

        dec_str = decision.decision.lower()
        match dec_str:
            case "approve":
                req.status = ApprovalStatus.APPROVED
                req.decision = "approve"
            case "reject":
                req.status = ApprovalStatus.REJECTED
                req.decision = "reject"
            case "modify":
                req.status = ApprovalStatus.MODIFIED
                req.decision = "modify"
                req.modified_arguments = decision.modified_arguments
            case _:
                req.status = ApprovalStatus.REJECTED
                req.decision = "reject"

        req.resolved_at = time.time()
        req.resolution_reason = decision.reason
        logger.info(
            f"HITL Request [{approval_id}] resolved: status={req.status.value}, reason='{decision.reason}'"
        )
        return req

    def clear(self) -> None:
        """Clear all stored approval requests (primarily for test isolation)."""
        self._requests.clear()


_global_hitl_manager: HITLManager | None = None


def get_hitl_manager() -> HITLManager:
    """Retrieve the singleton HITLManager instance."""
    global _global_hitl_manager
    if _global_hitl_manager is None:
        _global_hitl_manager = HITLManager()
    return _global_hitl_manager
