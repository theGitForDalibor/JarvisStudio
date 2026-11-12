"""SQLite persistence store for HITL approval leases and tool execution outcomes."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
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
from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.persistence.migrations import run_app_migrations
from jarvis_studio.core.persistence.sqlite import (
    initialize_sqlite_database,
    is_memory_db_path,
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)
from jarvis_studio.tools.base import RiskLevel


class DuplicateToolCallError(RuntimeError):
    """Raised when a `(thread_id, tool_call_id)` already has a persisted record."""

    def __init__(
        self,
        message: str,
        *,
        existing_outcome: ExecutionOutcome | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_outcome = existing_outcome


def _validate_non_empty_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str):
        raise TypeError("thread_id must be a non-empty string")
    cleaned = thread_id.strip()
    if not cleaned or cleaned.lower() == "default":
        raise ValueError(
            f"Invalid thread_id {thread_id!r}: must be non-empty and cannot be 'default'"
        )
    return cleaned


def _validate_non_empty_field(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _serialize_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _row_to_approval(row: sqlite3.Row) -> ApprovalRequest:
    raw_args = json.loads(row["arguments_json"])
    raw_mod = (
        json.loads(row["modified_arguments_json"])
        if row["modified_arguments_json"] is not None
        else None
    )
    return ApprovalRequest(
        id=row["id"],
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        checkpoint_id=row["checkpoint_id"],
        tool_name=row["tool_name"],
        arguments=raw_args if isinstance(raw_args, dict) else {},
        arguments_hash=row["arguments_hash"],
        tool_call_id=row["tool_call_id"],
        risk_level=RiskLevel(row["risk_level"]),
        status=ApprovalStatus(row["status"]),
        decision=row["decision"],
        description=row["description"],
        modified_arguments=raw_mod if isinstance(raw_mod, dict) else None,
        resolution_reason=row["resolution_reason"],
        lease_owner_instance_id=row["lease_owner_instance_id"],
        lease_acquired_run_id=row["lease_acquired_run_id"],
        execution_error=row["execution_error"],
        audit_note=row["audit_note"],
        audit_actor=row["audit_actor"],
        audited_at=row["audited_at"],
        created_at=float(row["created_at"]),
        resolved_at=row["resolved_at"],
        executed_at=row["executed_at"],
        completed_at=row["completed_at"],
    )


def _row_to_outcome(row: sqlite3.Row) -> ExecutionOutcome:
    raw_hint = (
        json.loads(row["side_effect_hint"])
        if row["side_effect_hint"] is not None
        else None
    )
    return ExecutionOutcome(
        outcome_id=row["outcome_id"],
        thread_id=row["thread_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        tool_call_id=row["tool_call_id"],
        approval_id=row["approval_id"],
        tool_name=row["tool_name"],
        is_high_risk=bool(row["is_high_risk"]),
        outcome_status=ExecutionOutcomeStatus(row["outcome_status"]),
        side_effect_hint=raw_hint if isinstance(raw_hint, dict) else None,
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        reconciled_by=row["reconciled_by"],
        reconciled_note=row["reconciled_note"],
        reconciled_at=row["reconciled_at"],
        started_at=float(row["started_at"]),
        finished_at=row["finished_at"],
        updated_at=float(row["updated_at"]),
    )


class HITLStore:
    """SQLite-backed store for `hitl_approval_requests` and `tool_execution_outcomes`."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        raw_path = (
            db_path
            if db_path is not None
            else get_settings().storage.sqlite_path
        )
        self.db_path = normalize_db_path(raw_path)
        self._is_memory = is_memory_db_path(self.db_path)
        self._in_memory_conn: sqlite3.Connection | None = (
            open_sync_connection(":memory:", enable_foreign_keys=True)
            if self._is_memory
            else None
        )
        self._init_db()

    def _init_db(self) -> None:
        if self._is_memory:
            run_app_migrations(self.db_path, in_memory_conn=self._in_memory_conn)
            return
        initialize_sqlite_database(self.db_path, enable_wal=True, enable_foreign_keys=True)
        run_app_migrations(self.db_path)

    @contextmanager
    def _read_conn(self):
        if self._is_memory:
            if self._in_memory_conn is None:
                raise RuntimeError("In-memory HITLStore connection is closed")
            yield self._in_memory_conn
            return
        conn = open_sync_connection(self.db_path, enable_foreign_keys=True)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self):
        if self._is_memory and self._in_memory_conn is None:
            raise RuntimeError("In-memory HITLStore connection is closed")
        with sync_transaction(
            self.db_path,
            in_memory_conn=self._in_memory_conn,
            enable_foreign_keys=True,
        ) as conn:
            yield conn

    def close(self) -> None:
        """Close owned `:memory:` SQLite connection cleanly and idempotently."""
        if self._in_memory_conn is not None:
            self._in_memory_conn.close()
            self._in_memory_conn = None

    def create_request(
        self,
        thread_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str,
        risk_level: RiskLevel = RiskLevel.HIGH,
        description: str = "",
        *,
        task_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> ApprovalRequest:
        """Persist a new `PENDING` approval request, enforcing thread/call uniqueness."""
        clean_tid = _validate_non_empty_thread_id(thread_id)
        clean_run = _validate_non_empty_field(run_id, "run_id")
        clean_call = _validate_non_empty_field(tool_call_id, "tool_call_id")
        clean_tool = _validate_non_empty_field(tool_name, "tool_name")

        args_dict = dict(arguments or {})
        args_json = _serialize_json(args_dict)
        args_hash = compute_arguments_hash(args_dict)
        desc = description or f"Execution of dangerous tool '{clean_tool}'"
        now = time.time()
        req = ApprovalRequest(
            thread_id=clean_tid,
            run_id=clean_run,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            tool_name=clean_tool,
            arguments=args_dict,
            arguments_hash=args_hash,
            tool_call_id=clean_call,
            risk_level=risk_level,
            description=desc,
            created_at=now,
        )

        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO hitl_approval_requests (
                        id, thread_id, task_id, run_id, checkpoint_id,
                        tool_call_id, tool_name, arguments_json, arguments_hash,
                        risk_level, status, decision, description,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        req.id,
                        req.thread_id,
                        req.task_id,
                        req.run_id,
                        req.checkpoint_id,
                        req.tool_call_id,
                        req.tool_name,
                        args_json,
                        req.arguments_hash,
                        req.risk_level.value,
                        req.status.value,
                        req.decision,
                        req.description,
                        req.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateToolCallError(
                f"Duplicate approval request for thread='{clean_tid}', tool_call_id='{clean_call}'"
            ) from exc

        return req

    def get_request(
        self, approval_id: str, thread_id: str | None = None
    ) -> ApprovalRequest | None:
        if not approval_id:
            return None
        with self._read_conn() as conn:
            if thread_id is not None:
                row = conn.execute(
                    "SELECT * FROM hitl_approval_requests WHERE id = ? AND thread_id = ?",
                    (approval_id, thread_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM hitl_approval_requests WHERE id = ?",
                    (approval_id,),
                ).fetchone()
        return _row_to_approval(row) if row is not None else None

    def get_by_tool_call_id(
        self, tool_call_id: str, thread_id: str | None = None
    ) -> ApprovalRequest | None:
        if not thread_id or not tool_call_id:
            return None
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM hitl_approval_requests
                WHERE thread_id = ? AND tool_call_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (thread_id, tool_call_id),
            ).fetchone()
        return _row_to_approval(row) if row is not None else None

    def list_pending(self, thread_id: str | None = None) -> list[ApprovalRequest]:
        with self._read_conn() as conn:
            if thread_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM hitl_approval_requests
                    WHERE status = 'PENDING' AND thread_id = ?
                    ORDER BY created_at ASC
                    """,
                    (thread_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM hitl_approval_requests
                    WHERE status = 'PENDING'
                    ORDER BY created_at ASC
                    """
                ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def list_recoverable_requests(self) -> list[ApprovalRequest]:
        """List non-terminal `PENDING`, `APPROVED`, and `MODIFIED` requests for recovery."""
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM hitl_approval_requests
                WHERE status IN ('PENDING', 'APPROVED', 'MODIFIED')
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def resolve_request(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        thread_id: str | None = None,
    ) -> ApprovalRequest | None:
        now = time.time()
        dec_str = decision.decision.lower()
        if dec_str == "approve":
            new_status = ApprovalStatus.APPROVED.value
            norm_decision = "approve"
            mod_json = None
        elif dec_str == "modify":
            new_status = ApprovalStatus.MODIFIED.value
            norm_decision = "modify"
            mod_json = (
                _serialize_json(decision.modified_arguments)
                if decision.modified_arguments is not None
                else None
            )
        else:
            new_status = ApprovalStatus.REJECTED.value
            norm_decision = "reject"
            mod_json = None

        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                return None

            if thread_id is not None and row["thread_id"] != thread_id:
                raise PermissionError(
                    f"Thread mismatch: Approval request '{approval_id}' belongs to thread '{row['thread_id']}', not '{thread_id}'"
                )

            if row["status"] != ApprovalStatus.PENDING.value:
                raise ValueError(
                    f"Approval request '{approval_id}' has already been resolved with status '{row['status']}'"
                )

            conn.execute(
                """
                UPDATE hitl_approval_requests
                SET status = ?,
                    decision = ?,
                    modified_arguments_json = ?,
                    resolved_at = ?,
                    resolution_reason = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (
                    new_status,
                    norm_decision,
                    mod_json,
                    now,
                    decision.reason,
                    approval_id,
                ),
            )
            updated_row = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return _row_to_approval(updated_row) if updated_row is not None else None

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
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE hitl_approval_requests
                SET status = 'EXECUTING',
                    executed_at = ?,
                    lease_owner_instance_id = ?,
                    lease_acquired_run_id = ?
                WHERE id = ?
                  AND thread_id = ?
                  AND tool_call_id = ?
                  AND arguments_hash = ?
                  AND status IN ('APPROVED', 'MODIFIED')
                """,
                (
                    now,
                    lease_owner_instance_id,
                    lease_acquired_run_id,
                    approval_id,
                    thread_id,
                    tool_call_id,
                    arguments_hash,
                ),
            )
            if cur.rowcount == 1:
                row = conn.execute(
                    "SELECT * FROM hitl_approval_requests WHERE id = ?",
                    (approval_id,),
                ).fetchone()
                assert row is not None
                req = _row_to_approval(row)
                outcome_id = f"out_{uuid.uuid4().hex[:12]}"
                effective_run_id = (
                    lease_acquired_run_id.strip()
                    if isinstance(lease_acquired_run_id, str)
                    and lease_acquired_run_id.strip()
                    else req.run_id
                )
                conn.execute(
                    """
                    INSERT INTO tool_execution_outcomes (
                        outcome_id, thread_id, task_id, run_id, tool_call_id,
                        approval_id, tool_name, is_high_risk, outcome_status,
                        started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'NOT_STARTED', ?, ?)
                    """,
                    (
                        outcome_id,
                        req.thread_id,
                        req.task_id,
                        effective_run_id,
                        req.tool_call_id,
                        req.id,
                        req.tool_name,
                        now,
                        now,
                    ),
                )
                return True, req, None

            existing = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()

        if existing is None:
            return False, None, f"Approval request '{approval_id}' not found"
        if existing["thread_id"] != thread_id:
            return (
                False,
                None,
                f"Thread mismatch: request belongs to '{existing['thread_id']}', not caller '{thread_id}'",
            )
        if existing["tool_call_id"] != tool_call_id:
            return (
                False,
                None,
                f"Tool call ID mismatch: request belongs to '{existing['tool_call_id']}', not caller '{tool_call_id}'",
            )
        if existing["arguments_hash"] != arguments_hash:
            return (
                False,
                None,
                f"Arguments hash mismatch: expected '{existing['arguments_hash']}', got '{arguments_hash}'",
            )

        status_val = existing["status"]
        if status_val == ApprovalStatus.EXECUTING.value:
            msg = f"Approval request '{approval_id}' is already EXECUTING (concurrent execution blocked)"
        elif status_val == ApprovalStatus.CONSUMED.value:
            msg = f"Approval request '{approval_id}' has already been CONSUMED (replay blocked)"
        elif status_val == ApprovalStatus.FAILED.value:
            msg = f"Approval request '{approval_id}' execution previously FAILED (replay blocked)"
        elif status_val == ApprovalStatus.UNKNOWN.value:
            msg = f"Approval request '{approval_id}' is locked in UNKNOWN (human audit required, replay blocked)"
        elif status_val == ApprovalStatus.REJECTED.value:
            msg = f"Approval request '{approval_id}' was REJECTED by user"
        elif status_val == ApprovalStatus.PENDING.value:
            msg = f"Approval request '{approval_id}' is still PENDING approval"
        else:
            msg = f"Approval request '{approval_id}' has invalid status '{status_val}'"
        return False, None, msg

    def finalize_execution(
        self,
        approval_id: str,
        success: bool,
        error: str | None = None,
    ) -> ApprovalRequest | None:
        """Atomically transition `EXECUTING` -> `CONSUMED` (`SUCCEEDED`) or `FAILED` (`FAILED`)."""
        now = time.time()
        new_status = (
            ApprovalStatus.CONSUMED.value if success else ApprovalStatus.FAILED.value
        )
        new_outcome = (
            ExecutionOutcomeStatus.SUCCEEDED.value
            if success
            else ExecutionOutcomeStatus.FAILED.value
        )
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                return None

            if row["status"] == ApprovalStatus.EXECUTING.value:
                conn.execute(
                    """
                    UPDATE hitl_approval_requests
                    SET status = ?,
                        execution_error = ?,
                        completed_at = ?
                    WHERE id = ? AND status = 'EXECUTING'
                    """,
                    (new_status, None if success else error, now, approval_id),
                )
                conn.execute(
                    """
                    UPDATE tool_execution_outcomes
                    SET outcome_status = ?,
                        error_message = ?,
                        finished_at = ?,
                        updated_at = ?
                    WHERE approval_id = ?
                    """,
                    (new_outcome, None if success else error, now, now, approval_id),
                )
                updated_row = conn.execute(
                    "SELECT * FROM hitl_approval_requests WHERE id = ?",
                    (approval_id,),
                ).fetchone()
                return _row_to_approval(updated_row) if updated_row is not None else None

            return _row_to_approval(row)

    def mark_outcome_uncertain(
        self,
        *,
        approval_id: str | None = None,
        thread_id: str | None = None,
        tool_call_id: str | None = None,
        error: str | None = None,
    ) -> ExecutionOutcome | None:
        """Mark an outcome as `UNKNOWN` (Option B) without altering `ApprovalStatus.FAILED`."""
        now = time.time()
        with self._tx() as conn:
            if approval_id is not None:
                conn.execute(
                    """
                    UPDATE tool_execution_outcomes
                    SET outcome_status = 'UNKNOWN',
                        error_message = COALESCE(?, error_message),
                        finished_at = COALESCE(finished_at, ?),
                        updated_at = ?
                    WHERE approval_id = ?
                    """,
                    (error, now, now, approval_id),
                )
                row = conn.execute(
                    "SELECT * FROM tool_execution_outcomes WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
            elif thread_id is not None and tool_call_id is not None:
                conn.execute(
                    """
                    UPDATE tool_execution_outcomes
                    SET outcome_status = 'UNKNOWN',
                        error_message = COALESCE(?, error_message),
                        finished_at = COALESCE(finished_at, ?),
                        updated_at = ?
                    WHERE thread_id = ? AND tool_call_id = ?
                    """,
                    (error, now, now, thread_id, tool_call_id),
                )
                row = conn.execute(
                    "SELECT * FROM tool_execution_outcomes WHERE thread_id = ? AND tool_call_id = ?",
                    (thread_id, tool_call_id),
                ).fetchone()
            else:
                return None
        return _row_to_outcome(row) if row is not None else None

    def register_safe_outcome_start(
        self,
        thread_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        *,
        task_id: str | None = None,
    ) -> ExecutionOutcome:
        """Persist `NOT_STARTED` before executing a tracked SAFE tool; block duplicates via UNIQUE."""
        clean_tid = _validate_non_empty_thread_id(thread_id)
        clean_run = _validate_non_empty_field(run_id, "run_id")
        clean_call = _validate_non_empty_field(tool_call_id, "tool_call_id")
        clean_tool = _validate_non_empty_field(tool_name, "tool_name")

        now = time.time()
        outcome = ExecutionOutcome(
            thread_id=clean_tid,
            task_id=task_id,
            run_id=clean_run,
            tool_call_id=clean_call,
            approval_id=None,
            tool_name=clean_tool,
            is_high_risk=False,
            outcome_status=ExecutionOutcomeStatus.NOT_STARTED,
            started_at=now,
            updated_at=now,
        )
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO tool_execution_outcomes (
                        outcome_id, thread_id, task_id, run_id, tool_call_id,
                        approval_id, tool_name, is_high_risk, outcome_status,
                        started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, 0, 'NOT_STARTED', ?, ?)
                    """,
                    (
                        outcome.outcome_id,
                        outcome.thread_id,
                        outcome.task_id,
                        outcome.run_id,
                        outcome.tool_call_id,
                        outcome.tool_name,
                        outcome.started_at,
                        outcome.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.get_outcome(clean_tid, clean_call)
            status_desc = (
                existing.outcome_status.value if existing is not None else "EXISTING"
            )
            raise DuplicateToolCallError(
                f"Tool call '{clean_call}' on thread '{clean_tid}' already has outcome '{status_desc}' (replay blocked)",
                existing_outcome=existing,
            ) from exc

        return outcome

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
        now = time.time()
        hint_json = _serialize_json(side_effect_hint) if side_effect_hint is not None else None
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE tool_execution_outcomes
                SET outcome_status = ?,
                    result_summary = COALESCE(?, result_summary),
                    error_message = COALESCE(?, error_message),
                    side_effect_hint = COALESCE(?, side_effect_hint),
                    finished_at = ?,
                    updated_at = ?
                WHERE thread_id = ? AND tool_call_id = ? AND approval_id IS NULL
                """,
                (
                    outcome_status.value,
                    result_summary,
                    error_message,
                    hint_json,
                    now,
                    now,
                    thread_id,
                    tool_call_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tool_execution_outcomes WHERE thread_id = ? AND tool_call_id = ?",
                (thread_id, tool_call_id),
            ).fetchone()
        return _row_to_outcome(row) if row is not None else None

    def get_outcome(self, thread_id: str, tool_call_id: str) -> ExecutionOutcome | None:
        if not thread_id or not tool_call_id:
            return None
        with self._read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tool_execution_outcomes WHERE thread_id = ? AND tool_call_id = ?",
                (thread_id, tool_call_id),
            ).fetchone()
        return _row_to_outcome(row) if row is not None else None

    def get_outcome_by_approval(
        self, approval_id: str, thread_id: str | None = None
    ) -> ExecutionOutcome | None:
        if not approval_id:
            return None
        with self._read_conn() as conn:
            if thread_id is not None:
                row = conn.execute(
                    "SELECT * FROM tool_execution_outcomes WHERE approval_id = ? AND thread_id = ?",
                    (approval_id, thread_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tool_execution_outcomes WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
        return _row_to_outcome(row) if row is not None else None

    def list_outcomes(self, thread_id: str) -> list[ExecutionOutcome]:
        if not thread_id:
            return []
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tool_execution_outcomes
                WHERE thread_id = ?
                ORDER BY started_at ASC
                """,
                (thread_id,),
            ).fetchall()
        return [_row_to_outcome(row) for row in rows]

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
        """Record human audit reconciliation on an `UNKNOWN` outcome without reactivating leases."""
        if verified_status not in (
            ExecutionOutcomeStatus.SUCCEEDED,
            ExecutionOutcomeStatus.FAILED,
            ExecutionOutcomeStatus.PARTIAL,
        ):
            raise ValueError(
                f"Invalid human audit target status: {verified_status.value}"
            )
        clean_tid = _validate_non_empty_thread_id(thread_id)
        clean_actor = _validate_non_empty_field(actor, "actor")
        now = time.time()

        with self._tx() as conn:
            if approval_id is not None:
                row = conn.execute(
                    """
                    SELECT * FROM tool_execution_outcomes
                    WHERE approval_id = ? AND thread_id = ?
                    """,
                    (approval_id, clean_tid),
                ).fetchone()
            elif tool_call_id is not None:
                row = conn.execute(
                    """
                    SELECT * FROM tool_execution_outcomes
                    WHERE thread_id = ? AND tool_call_id = ?
                    """,
                    (clean_tid, tool_call_id),
                ).fetchone()
            else:
                return None

            if row is None:
                return None

            conn.execute(
                """
                UPDATE tool_execution_outcomes
                SET outcome_status = ?,
                    reconciled_by = ?,
                    reconciled_note = ?,
                    reconciled_at = ?,
                    updated_at = ?
                WHERE outcome_id = ?
                """,
                (
                    verified_status.value,
                    clean_actor,
                    note,
                    now,
                    now,
                    row["outcome_id"],
                ),
            )
            linked_appr_id = row["approval_id"] or approval_id
            if linked_appr_id is not None:
                # Never alter hitl_approval_requests.status; record audit metadata only.
                conn.execute(
                    """
                    UPDATE hitl_approval_requests
                    SET audit_note = ?,
                        audit_actor = ?,
                        audited_at = ?
                    WHERE id = ? AND thread_id = ?
                    """,
                    (note, clean_actor, now, linked_appr_id, clean_tid),
                )

            updated = conn.execute(
                "SELECT * FROM tool_execution_outcomes WHERE outcome_id = ?",
                (row["outcome_id"],),
            ).fetchone()
        return _row_to_outcome(updated) if updated is not None else None

    def mark_stale_executing_unknown(self, current_runtime_id: str) -> int:
        """Converge stale `EXECUTING` leases from dead runtimes to `Approval=UNKNOWN, Outcome=UNKNOWN`."""
        now = time.time()
        with self._tx() as conn:
            stale_rows = conn.execute(
                """
                SELECT id FROM hitl_approval_requests
                WHERE status = 'EXECUTING'
                  AND (lease_owner_instance_id IS NULL OR lease_owner_instance_id != ?)
                """,
                (current_runtime_id,),
            ).fetchall()
            if not stale_rows:
                return 0
            stale_ids = [str(r["id"]) for r in stale_rows]
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"""
                UPDATE hitl_approval_requests
                SET status = 'UNKNOWN',
                    execution_error = COALESCE(execution_error, 'runtime_restarted_while_executing'),
                    completed_at = COALESCE(completed_at, ?)
                WHERE id IN ({placeholders})
                """,
                (now, *stale_ids),
            )
            conn.execute(
                f"""
                UPDATE tool_execution_outcomes
                SET outcome_status = 'UNKNOWN',
                    error_message = COALESCE(error_message, 'runtime_restarted_while_executing'),
                    finished_at = COALESCE(finished_at, ?),
                    updated_at = ?
                WHERE approval_id IN ({placeholders})
                """,
                (now, now, *stale_ids),
            )
            return len(stale_ids)

    def mark_stale_safe_outcomes_unknown(self) -> int:
        """Converge pre-existing SAFE `NOT_STARTED` outcomes from dead runtimes to `UNKNOWN`."""
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE tool_execution_outcomes
                SET outcome_status = 'UNKNOWN',
                    error_message = COALESCE(error_message, 'runtime_restarted_while_not_started'),
                    finished_at = COALESCE(finished_at, ?),
                    updated_at = ?
                WHERE approval_id IS NULL AND outcome_status = 'NOT_STARTED'
                """,
                (now, now),
            )
            return int(cur.rowcount or 0)

    def mark_orphan_pending_failed(self, approval_id: str, reason: str) -> bool:
        """Fail-closed orphan `PENDING` request lacking an active checkpoint interrupt."""
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE hitl_approval_requests
                SET status = 'FAILED',
                    execution_error = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE id = ? AND status = 'PENDING'
                """,
                (reason, now, approval_id),
            )
            return cur.rowcount == 1

    def abort_approved_request(
        self,
        approval_id: str,
        thread_id: str,
        reason: str | None = None,
    ) -> ApprovalRequest | None:
        """Explicitly abort an unleased `APPROVED`/`MODIFIED` request into terminal `FAILED`."""
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE hitl_approval_requests
                SET status = 'FAILED',
                    decision = 'reject',
                    resolution_reason = COALESCE(?, resolution_reason),
                    execution_error = 'aborted_before_execution',
                    completed_at = ?
                WHERE id = ?
                  AND thread_id = ?
                  AND status IN ('APPROVED', 'MODIFIED')
                  AND lease_owner_instance_id IS NULL
                """,
                (reason, now, approval_id, thread_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM hitl_approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return _row_to_approval(row) if row is not None else None

    def clear(self) -> None:
        """Delete all stored approvals and outcomes (for test isolation)."""
        with self._tx() as conn:
            conn.execute("DELETE FROM tool_execution_outcomes")
            conn.execute("DELETE FROM hitl_approval_requests")
