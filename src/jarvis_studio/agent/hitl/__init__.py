"""Human-in-the-Loop approval package."""

from jarvis_studio.agent.hitl.authorization import (
    AuthorizationResult,
    verify_tool_execution_authorization,
)
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    HITLManager,
    compute_arguments_hash,
    get_hitl_manager,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuthorizationResult",
    "HITLManager",
    "compute_arguments_hash",
    "get_hitl_manager",
    "verify_tool_execution_authorization",
]
