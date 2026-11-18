"""Lock-gated Startup Recovery & Reconciliation for Phase 3 HITL/Outcome state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis_studio.agent.hitl.models import ApprovalStatus
from jarvis_studio.core.persistence.runtime_lock import RUNTIME_LOCK_FILENAME
from jarvis_studio.core.persistence.sqlite import is_memory_db_path, normalize_db_path

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

    from jarvis_studio.agent.hitl.store import HITLStore
    from jarvis_studio.core.persistence.runtime_lock import RuntimeInstanceLock

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartupRecoveryReport:
    """Summary of idempotent startup reconciliation actions."""

    runtime_instance_id: str
    stale_executing_marked_unknown: int = 0
    stale_safe_outcomes_marked_unknown: int = 0
    orphan_pending_marked_failed: int = 0
    kept_pending: int = 0
    kept_approved_for_explicit_resume: int = 0
    stale_tasks_reconciled: int = 0
    stale_runs_reconciled: int = 0


def _extract_active_interrupt_approval_ids(
    tup: CheckpointTuple | None,
    expected_thread_id: str,
) -> set[str]:
    """Extract approval IDs present in active `__interrupt__` pending writes of a checkpoint."""
    if tup is None:
        return set()

    checkpoint = tup.checkpoint or {}
    channel_values = checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
    if isinstance(channel_values, dict):
        run_ctx = channel_values.get("run_context")
        if isinstance(run_ctx, dict):
            sess_id = run_ctx.get("session_id")
            if sess_id != expected_thread_id:
                return set()

    active_ids: set[str] = set()
    for pw in tup.pending_writes or ():
        if len(pw) < 3 or pw[1] != "__interrupt__":
            continue
        interrupts = pw[2]
        if not isinstance(interrupts, (list, tuple)):
            interrupts = (interrupts,)
        for intr in interrupts:
            val: Any = getattr(intr, "value", intr)
            if not isinstance(val, dict):
                continue
            if val.get("thread_id") != expected_thread_id:
                continue
            primary_id = val.get("approval_id")
            if isinstance(primary_id, str) and primary_id.strip():
                active_ids.add(primary_id.strip())
            for sub in val.get("requests") or ():
                if (
                    isinstance(sub, dict)
                    and sub.get("thread_id") == expected_thread_id
                    and isinstance(sub.get("approval_id"), str)
                    and sub["approval_id"].strip()
                ):
                    active_ids.add(sub["approval_id"].strip())
    return active_ids


async def reconcile_on_startup(
    *,
    hitl_store: HITLStore,
    current_runtime_id: str,
    checkpointer: BaseCheckpointSaver | None = None,
    check_checkpoints: bool = False,
    runtime_lock: RuntimeInstanceLock | None = None,
    task_coordinator: Any | None = None,
) -> StartupRecoveryReport:
    """Reconcile stale HITL leases, outcomes, tasks, and runs safely under `RuntimeInstanceLock`."""
    if runtime_lock is None:
        raise RuntimeError(
            "Startup recovery refused: RuntimeInstanceLock is required before reconciling DB state"
        )

    is_memory = is_memory_db_path(hitl_store.db_path)
    if runtime_lock.is_noop:
        if not is_memory:
            raise RuntimeError(
                "Startup recovery refused: Noop RuntimeInstanceLock is only permitted for in-memory databases"
            )
    else:
        if is_memory:
            raise RuntimeError(
                "Startup recovery refused: file-backed RuntimeInstanceLock cannot be used for in-memory databases"
            )
        if not runtime_lock.is_acquired:
            raise RuntimeError(
                "Startup recovery refused: RuntimeInstanceLock must be acquired before reconciling DB state"
            )
        db_canonical_parent = Path(normalize_db_path(hitl_store.db_path)).resolve().parent
        expected_lock = (db_canonical_parent / RUNTIME_LOCK_FILENAME).resolve()
        if not any(lp.resolve() == expected_lock for lp in runtime_lock.lock_paths):
            raise RuntimeError(
                f"Startup recovery refused: RuntimeInstanceLock does not cover HITLStore database at '{hitl_store.db_path}'"
            )

    stale_exec = hitl_store.mark_stale_executing_unknown(current_runtime_id)
    stale_safe = hitl_store.mark_stale_safe_outcomes_unknown()

    orphan_failed = 0
    kept_pending = 0
    kept_approved = 0

    if check_checkpoints and checkpointer is not None:
        recoverable = hitl_store.list_recoverable_requests()
        by_thread: dict[str, list[Any]] = {}
        for req in recoverable:
            by_thread.setdefault(req.thread_id, []).append(req)

        for tid, reqs in by_thread.items():
            tup = await checkpointer.aget_tuple({"configurable": {"thread_id": tid}})
            active_ids = _extract_active_interrupt_approval_ids(tup, tid)
            for req in reqs:
                if req.id in active_ids:
                    if req.status == ApprovalStatus.PENDING:
                        kept_pending += 1
                    elif (
                        req.status in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED)
                        and req.lease_owner_instance_id is None
                    ):
                        kept_approved += 1
                else:
                    if req.status == ApprovalStatus.PENDING:
                        if hitl_store.mark_orphan_pending_failed(
                            req.id, "missing_active_checkpoint_interrupt"
                        ):
                            orphan_failed += 1
                    elif (
                        req.status in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED)
                        and req.lease_owner_instance_id is None
                        and hitl_store.abort_approved_request(
                            req.id,
                            req.thread_id,
                            "missing_active_checkpoint_interrupt",
                        )
                        is not None
                    ):
                        orphan_failed += 1

    # Reconcile stale tasks and runs from prior crashes or ungraceful termination
    if task_coordinator is None:
        try:
            from jarvis_studio.core.tasks.coordinator import get_task_coordinator
            task_coordinator = get_task_coordinator(hitl_store.db_path)
        except Exception:
            task_coordinator = None

    stale_tasks = 0
    stale_runs = 0
    if task_coordinator is not None:
        try:
            stale_tasks, stale_runs = task_coordinator.reconcile_stale_tasks_and_runs(current_runtime_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Failed to reconcile stale tasks and runs on startup: %s", exc)

    report = StartupRecoveryReport(
        runtime_instance_id=current_runtime_id,
        stale_executing_marked_unknown=stale_exec,
        stale_safe_outcomes_marked_unknown=stale_safe,
        orphan_pending_marked_failed=orphan_failed,
        kept_pending=kept_pending,
        kept_approved_for_explicit_resume=kept_approved,
        stale_tasks_reconciled=stale_tasks,
        stale_runs_reconciled=stale_runs,
    )
    if (
        stale_exec
        or stale_safe
        or orphan_failed
        or kept_pending
        or kept_approved
        or stale_tasks
        or stale_runs
    ):
        _logger.info("Startup HITL & Task recovery completed: %s", report)
    return report
