"""Human-in-the-Loop (HITL) approval manager backed by SQLite `HITLStore`."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from jarvis_studio.agent.hitl.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    compute_arguments_hash,
)
from jarvis_studio.agent.hitl.store import DuplicateToolCallError, HITLStore
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import RiskLevel

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStatus",
    "DuplicateToolCallError",
    "ExecutionOutcome",
    "ExecutionOutcomeStatus",
    "HITLManager",
    "compute_arguments_hash",
    "get_hitl_manager",
]


class HITLManager:
    """Thread-safe approval & execution outcome manager persisted via SQLite `HITLStore`."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        runtime_instance_id: str | None = None,
    ) -> None:
        self._store = HITLStore(db_path=db_path)
        self._runtime_instance_id = runtime_instance_id
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    @property
    def store(self) -> HITLStore:
        """Underlying SQLite `HITLStore`."""
        return self._store

    @property
    def db_path(self) -> str:
        """Normalized SQLite path used by this manager."""
        return self._store.db_path

    @property
    def runtime_instance_id(self) -> str | None:
        """Active runtime instance ID stamped on acquired execution leases."""
        return self._runtime_instance_id

    @runtime_instance_id.setter
    def runtime_instance_id(self, value: str | None) -> None:
        self._runtime_instance_id = value

    def _sync_cached(self, req: ApprovalRequest | None) -> ApprovalRequest | None:
        if req is None:
            return None
        cached = self._requests.get(req.id)
        if cached is None:
            self._requests[req.id] = req
            return req
        for field_name in ApprovalRequest.model_fields:
            setattr(cached, field_name, getattr(req, field_name))
        return cached

    def create_request(
        self,
        thread_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str = "",
        risk_level: RiskLevel = RiskLevel.HIGH,
        description: str = "",
        *,
        task_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> ApprovalRequest:
        """Create and persist a new `PENDING` approval request."""
        with self._lock:
            req = self._store.create_request(
                thread_id=thread_id,
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                tool_call_id=tool_call_id,
                risk_level=risk_level,
                description=description,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
            )
            synced = self._sync_cached(req)
            assert synced is not None

        logger.warning(
            f"HITL Interrupt created: [{synced.id}] (call_id: '{tool_call_id}') "
            f"for tool '{tool_name}' on thread '{thread_id}'"
        )
        return synced

    def get_request(
        self, approval_id: str, thread_id: str | None = None
    ) -> ApprovalRequest | None:
        """Look up an approval request by ID, optionally enforcing thread boundary."""
        with self._lock:
            req = self._store.get_request(approval_id, thread_id=thread_id)
            return self._sync_cached(req)

    def get_by_tool_call_id(
        self, tool_call_id: str, thread_id: str | None = None
    ) -> ApprovalRequest | None:
        """Find the approval request matching `tool_call_id` strictly within `thread_id`."""
        with self._lock:
            req = self._store.get_by_tool_call_id(tool_call_id, thread_id=thread_id)
            return self._sync_cached(req)

    def list_pending(self, thread_id: str | None = None) -> list[ApprovalRequest]:
        """List all pending approval requests, optionally filtered by thread ID."""
        with self._lock:
            rows = self._store.list_pending(thread_id=thread_id)
            return [r for r in (self._sync_cached(row) for row in rows) if r is not None]

    def resolve_request(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        thread_id: str | None = None,
    ) -> ApprovalRequest | None:
        """Apply user decision to resolve a pending approval."""
        with self._lock:
            req = self._store.resolve_request(
                approval_id=approval_id,
                decision=decision,
                thread_id=thread_id,
            )
            synced = self._sync_cached(req)

        if synced is not None:
            logger.info(
                f"HITL Request [{approval_id}] resolved: status={synced.status.value}, "
                f"reason='{decision.reason}'"
            )
        return synced

    def acquire_execution_lease(
        self,
        approval_id: str,
        thread_id: str,
        tool_call_id: str,
        arguments_hash: str,
        *,
        lease_owner_instance_id: str | None = None,
        lease_acquired_run_id: str | None = None,
    ) -> tuple[bool, ApprovalRequest | None, str | None]:
        """Atomically transition `APPROVED`/`MODIFIED` -> `EXECUTING` and seed `NOT_STARTED` Outcome."""
        effective_owner = (
            lease_owner_instance_id
            if lease_owner_instance_id is not None
            else self._runtime_instance_id
        )
        with self._lock:
            ok, req, err = self._store.acquire_execution_lease(
                approval_id=approval_id,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                arguments_hash=arguments_hash,
                lease_owner_instance_id=effective_owner,
                lease_acquired_run_id=lease_acquired_run_id,
            )
            synced = self._sync_cached(req)
            return ok, synced, err

    def finalize_execution(
        self,
        approval_id: str,
        success: bool,
        error: str | None = None,
    ) -> ApprovalRequest | None:
        """Finalize execution status of a leased approval request.

        Transitions:
        - If success: `EXECUTING -> CONSUMED` (`Outcome -> SUCCEEDED`)
        - If failure: `EXECUTING -> FAILED` (`Outcome -> FAILED`)
        """
        with self._lock:
            existing = self._store.get_request(approval_id)
            if existing is None:
                logger.warning(
                    f"HITL Execution finalize skipped: approval [{approval_id}] not found"
                )
                return None

            prev_status = existing.status
            updated = self._store.finalize_execution(
                approval_id=approval_id,
                success=success,
                error=error,
            )
            synced = self._sync_cached(updated)

        if synced is not None:
            if prev_status == ApprovalStatus.EXECUTING:
                logger.info(
                    f"HITL Execution finalized for [{approval_id}]: status={synced.status.value}"
                )
            else:
                logger.warning(
                    f"HITL Execution finalize ignored for [{approval_id}]: "
                    f"expected EXECUTING, got {prev_status.value}"
                )
        return synced

    def mark_outcome_uncertain(
        self,
        *,
        approval_id: str | None = None,
        thread_id: str | None = None,
        tool_call_id: str | None = None,
        error: str | None = None,
    ) -> ExecutionOutcome | None:
        """Record `Outcome=UNKNOWN` (Option B) while keeping `ApprovalStatus` untouched."""
        with self._lock:
            return self._store.mark_outcome_uncertain(
                approval_id=approval_id,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                error=error,
            )

    def register_safe_outcome_start(
        self,
        thread_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        *,
        task_id: str | None = None,
    ) -> ExecutionOutcome:
        """Persist `NOT_STARTED` before executing a tracked SAFE tool."""
        with self._lock:
            return self._store.register_safe_outcome_start(
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                task_id=task_id,
            )

    def finalize_safe_outcome(
        self,
        thread_id: str,
        tool_call_id: str,
        outcome_status: ExecutionOutcomeStatus,
        *,
        result_summary: str | None = None,
        error_message: str | None = None,
        side_effect_hint: dict[str, Any] | None = None,
    ) -> ExecutionOutcome | None:
        """Finalize outcome status for a tracked SAFE tool call."""
        with self._lock:
            return self._store.finalize_safe_outcome(
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                outcome_status=outcome_status,
                result_summary=result_summary,
                error_message=error_message,
                side_effect_hint=side_effect_hint,
            )

    def get_outcome(self, thread_id: str, tool_call_id: str) -> ExecutionOutcome | None:
        with self._lock:
            return self._store.get_outcome(thread_id, tool_call_id)

    def get_outcome_by_approval(
        self, approval_id: str, thread_id: str | None = None
    ) -> ExecutionOutcome | None:
        with self._lock:
            return self._store.get_outcome_by_approval(approval_id, thread_id=thread_id)

    def list_outcomes(self, thread_id: str) -> list[ExecutionOutcome]:
        with self._lock:
            return self._store.list_outcomes(thread_id)

    def audit_uncertain_outcome(
        self,
        *,
        thread_id: str,
        verified_status: ExecutionOutcomeStatus,
        actor: str,
        note: str,
        approval_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ExecutionOutcome | None:
        """Record human audit reconciliation on an uncertain outcome without reactivating leases."""
        with self._lock:
            return self._store.audit_uncertain_outcome(
                thread_id=thread_id,
                verified_status=verified_status,
                actor=actor,
                note=note,
                approval_id=approval_id,
                tool_call_id=tool_call_id,
            )

    def abort_approved_request(
        self,
        approval_id: str,
        thread_id: str,
        reason: str | None = None,
    ) -> ApprovalRequest | None:
        """Explicitly abort an unleased `APPROVED`/`MODIFIED` request into `FAILED`."""
        with self._lock:
            req = self._store.abort_approved_request(
                approval_id=approval_id,
                thread_id=thread_id,
                reason=reason,
            )
            return self._sync_cached(req)

    def clear(self) -> None:
        """Clear all stored approval requests and outcomes (primarily for test isolation)."""
        with self._lock:
            self._store.clear()
            self._requests.clear()

    def close(self) -> None:
        """Close underlying SQLite store resources cleanly."""
        with self._lock:
            self._store.close()
            self._requests.clear()


_global_hitl_manager: HITLManager | None = None
_default_hitl_manager: HITLManager | None = None


def get_hitl_manager() -> HITLManager:
    """Retrieve explicit override, active AgentRuntime HITLManager, or fallback singleton."""
    global _default_hitl_manager
    if _global_hitl_manager is not None:
        return _global_hitl_manager

    from jarvis_studio.core.runtime.agent_runtime import get_active_runtime

    active_runtime = get_active_runtime()
    if active_runtime is not None and active_runtime._hitl_manager is not None:
        return active_runtime._hitl_manager

    if _default_hitl_manager is None:
        _default_hitl_manager = HITLManager()
    return _default_hitl_manager
