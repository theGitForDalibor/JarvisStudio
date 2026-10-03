"""Human-in-the-Loop (HITL) approval manager and schemas."""

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import RiskLevel


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
    arguments: Dict[str, Any]
    risk_level: RiskLevel = RiskLevel.HIGH
    status: ApprovalStatus = ApprovalStatus.PENDING
    description: str = "High-risk tool call intercepted"
    created_at: float = Field(default_factory=time.time)
    resolved_at: Optional[float] = None
    resolution_reason: Optional[str] = None
    modified_arguments: Optional[Dict[str, Any]] = None


class ApprovalDecision(BaseModel):
    """User decision payload to resolve a pending approval."""
    decision: str = Field(
        description="'approve', 'reject', or 'modify'",
    )
    reason: Optional[str] = None
    modified_arguments: Optional[Dict[str, Any]] = None


class HITLManager:
    """In-memory manager tracking active and historical approval requests."""

    def __init__(self) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}

    def create_request(
        self,
        thread_id: str,
        run_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        risk_level: RiskLevel = RiskLevel.HIGH,
        description: str = "",
    ) -> ApprovalRequest:
        """Create and record a new approval request."""
        req = ApprovalRequest(
            thread_id=thread_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            description=description or f"Execution of dangerous tool '{tool_name}'",
        )
        self._requests[req.id] = req
        logger.warning(
            f"HITL Interrupt created: [{req.id}] for tool '{tool_name}' on thread '{thread_id}'"
        )
        return req

    def get_request(self, approval_id: str) -> Optional[ApprovalRequest]:
        """Look up an approval request by its ID."""
        return self._requests.get(approval_id)

    def list_pending(self, thread_id: Optional[str] = None) -> List[ApprovalRequest]:
        """List all pending approval requests, optionally filtered by thread ID."""
        return [
            r for r in self._requests.values()
            if r.status == ApprovalStatus.PENDING and (thread_id is None or r.thread_id == thread_id)
        ]

    def resolve_request(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> Optional[ApprovalRequest]:
        """Apply user decision to resolve a pending approval."""
        req = self._requests.get(approval_id)
        if not req:
            return None

        match decision.decision.lower():
            case "approve":
                req.status = ApprovalStatus.APPROVED
            case "reject":
                req.status = ApprovalStatus.REJECTED
            case "modify":
                req.status = ApprovalStatus.MODIFIED
                req.modified_arguments = decision.modified_arguments
            case _:
                req.status = ApprovalStatus.REJECTED

        req.resolved_at = time.time()
        req.resolution_reason = decision.reason
        logger.info(
            f"HITL Request [{approval_id}] resolved: status={req.status.value}, reason='{decision.reason}'"
        )
        return req


_global_hitl_manager: Optional[HITLManager] = None


def get_hitl_manager() -> HITLManager:
    """Retrieve the singleton HITLManager instance."""
    global _global_hitl_manager
    if _global_hitl_manager is None:
        _global_hitl_manager = HITLManager()
    return _global_hitl_manager
