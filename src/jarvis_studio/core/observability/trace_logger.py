"""Agent Lifecycle Observability and White-Box Trace Suite.

Implements the agent-lifecycle-observability specification from the AI Engineering Skills knowledge base:
1. 5-stage business lifecycle timeline: input -> workflow -> preload -> thinking -> output
2. 3D Streaming latency metrics:
   - TTFT (Time To First Think): User request to first thinking state (<200ms)
   - TTFA (Time To First Answer): Time to first natural language token (<2.5s)
   - TTLT (Time To Last Token): End-to-end total execution latency
3. Fine-grained tool step classification: tool | mcp | model_think | model_output
4. Ground-truth argument alignment and async SQLite persistence into agent_conversation_traces.
"""

import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Self

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.persistence.migrations import run_app_migrations
from jarvis_studio.core.persistence.sqlite import (
    SyncSQLiteSession,
    initialize_sqlite_database,
)

_logger = logging.getLogger(__name__)

_SENSITIVE_KEY_TOKENS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "authorization",
    "access_token",
    "refresh_token",
    "cookie",
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(Bearer\s+[A-Za-z0-9\-_\.]+|sk-[A-Za-z0-9]{12,})", re.IGNORECASE
)


def sanitize_trace_data(data: Any, *, max_str_len: int = 8000) -> Any:
    """Recursively redact sensitive credentials and truncate oversized strings."""
    if data is None or isinstance(data, (bool, int, float)):
        return data
    if isinstance(data, str):
        redacted = _SECRET_VALUE_PATTERN.sub("[REDACTED]", data)
        if len(redacted) > max_str_len:
            return redacted[:max_str_len] + "... [truncated]"
        return redacted
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            lowered = key_str.lower()
            if lowered == "token" or lowered.endswith("_token") or any(
                tok in lowered for tok in _SENSITIVE_KEY_TOKENS
            ):
                sanitized[key_str] = "[REDACTED]"
            else:
                sanitized[key_str] = sanitize_trace_data(v, max_str_len=max_str_len)
        return sanitized
    if isinstance(data, (list, tuple)):
        return [sanitize_trace_data(item, max_str_len=max_str_len) for item in data]
    return sanitize_trace_data(str(data), max_str_len=max_str_len)


@dataclass
class ThinkingStep:
    """Detailed record of a single thinking or tool execution step."""

    seq: int
    step_type: str  # "tool" | "mcp" | "model_think" | "model_output"
    name: str
    arguments: Any = None
    result: Any = None
    error: str | None = None
    status: str = "success"  # "success" | "error"
    duration_ms: int = 0
    timestamp: float = field(default_factory=time.time)
    group: str | None = None


@dataclass
class ConversationTrace:
    """Full 5-stage business lifecycle trace for a single agent turn."""

    trace_id: str = field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:12]}")
    session_id: str | None = None
    run_id: str = ""
    task_id: str | None = None
    query: str = ""
    start_time: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # 1. input stage
    input_stage: dict[str, Any] = field(default_factory=dict)

    # 2. workflow / guard stage
    guard_stage: dict[str, Any] = field(default_factory=dict)

    # 3. preload stage
    preload_stage: dict[str, Any] = field(default_factory=dict)

    # 4. thinking & tool execution steps
    thinking_steps: list[ThinkingStep] = field(default_factory=list)

    # 5. output stage & 3D streaming metrics
    output_text: str = ""
    error: str | None = None
    ttft_ms: int | None = None  # Time To First Think
    ttfa_ms: int | None = None  # Time To First Answer
    ttlt_ms: int | None = None  # Time To Last Token
    total_duration_ms: int = 0
    status: str = "running"      # "completed" | "error" | "halted" | "rule_direct"

    def mark_ttft(self) -> None:
        """Record Time To First Think."""
        if self.ttft_ms is None:
            self.ttft_ms = int((time.time() - self.start_time) * 1000)

    def mark_ttfa(self) -> None:
        """Record Time To First Answer."""
        if self.ttfa_ms is None:
            self.ttfa_ms = int((time.time() - self.start_time) * 1000)

    def finish(self, output: str = "", error: str | None = None, status: str = "completed") -> None:
        """Finalize the trace with full durations."""
        now = time.time()
        self.ttlt_ms = int((now - self.start_time) * 1000)
        self.total_duration_ms = self.ttlt_ms
        self.output_text = output
        self.error = error
        self.status = status if not error else "error"
        self.updated_at = now

    def add_step(
        self,
        step_type: str,
        name: str,
        arguments: Any = None,
        result: Any = None,
        duration_ms: int = 0,
        status: str = "success",
        error: str | None = None,
        group: str | None = None,
    ) -> ThinkingStep:
        """Append a thinking step to the trace."""
        step = ThinkingStep(
            seq=len(self.thinking_steps) + 1,
            step_type=step_type,
            name=name,
            arguments=sanitize_trace_data(arguments),
            result=sanitize_trace_data(result),
            duration_ms=duration_ms,
            status=status,
            error=sanitize_trace_data(error) if error is not None else None,
            group=group,
        )
        self.thinking_steps.append(step)
        return step

    def update_last_tool_arguments(self, tool_name: str, actual_arguments: Any) -> bool:
        """Align the most recent matching tool step's arguments with ground-truth values."""
        for step in reversed(self.thinking_steps):
            if step.step_type in ("tool", "mcp") and step.name == tool_name:
                step.arguments = sanitize_trace_data(actual_arguments)
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """Convert trace to JSON-serializable dictionary."""
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "query": sanitize_trace_data(self.query),
            "start_time": self.start_time,
            "updated_at": self.updated_at,
            "status": self.status,
            "input_stage": sanitize_trace_data(self.input_stage),
            "guard_stage": sanitize_trace_data(self.guard_stage),
            "preload_stage": sanitize_trace_data(self.preload_stage),
            "thinking_steps": [asdict(s) for s in self.thinking_steps],
            "output_text": sanitize_trace_data(self.output_text, max_str_len=32000),
            "error": sanitize_trace_data(self.error) if self.error is not None else None,
            "ttft_ms": self.ttft_ms,
            "ttfa_ms": self.ttfa_ms,
            "ttlt_ms": self.ttlt_ms,
            "total_duration_ms": self.total_duration_ms,
        }


class TraceStore:
    """SQLite-backed persistent store for conversation lifecycle traces."""

    def __init__(self, db_path: str | None = None) -> None:
        resolved_path = db_path or get_settings().storage.sqlite_path
        self._db = SyncSQLiteSession(resolved_path, enable_foreign_keys=True)
        self._save_counter = 0
        self.db_path = self._db.db_path
        if not self._db.is_memory:
            initialize_sqlite_database(
                self.db_path, enable_wal=True, enable_foreign_keys=True
            )
            run_app_migrations(self.db_path)
        else:
            run_app_migrations(self.db_path, in_memory_conn=self._in_memory_conn)
        self._init_db()

    @property
    def _in_memory_conn(self) -> sqlite3.Connection | None:
        return self._db.in_memory_conn

    def close(self) -> None:
        """Idempotently release any private in-memory SQLite connection."""
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init_db(self) -> None:
        """Create traces table if not exists."""
        if not self._db.is_memory:
            with self._db.read_connection() as conn:
                has_table = (
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversation_traces'"
                    ).fetchone()
                    is not None
                )
            if has_table:
                return

        with self._db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_conversation_traces (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    run_id TEXT,
                    task_id TEXT,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    updated_at REAL,
                    total_duration_ms INTEGER,
                    ttft_ms INTEGER,
                    ttfa_ms INTEGER,
                    ttlt_ms INTEGER,
                    steps_count INTEGER,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_session ON agent_conversation_traces(session_id, start_time DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_task_id ON agent_conversation_traces(task_id)"
            )

    def save_trace(self, trace: ConversationTrace) -> None:
        """Persist completed trace into SQLite."""
        payload = json.dumps(trace.to_dict(), ensure_ascii=False)
        now = time.time()
        updated_at = trace.updated_at or now
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_conversation_traces (
                    trace_id, session_id, run_id, query, status,
                    start_time, total_duration_ms, ttft_ms, ttfa_ms, ttlt_ms,
                    steps_count, payload, task_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.session_id,
                    trace.run_id,
                    trace.query,
                    trace.status,
                    trace.start_time,
                    trace.total_duration_ms,
                    trace.ttft_ms,
                    trace.ttfa_ms,
                    trace.ttlt_ms,
                    len(trace.thinking_steps),
                    payload,
                    trace.task_id,
                    updated_at,
                ),
            )

    def maybe_prune_old_traces(
        self,
        every_n: int = 20,
        max_age_days: int = 30,
        max_rows_per_session: int = 200,
    ) -> int:
        """Periodically prune old traces (every_n saves) to avoid per-request table scans."""
        self._save_counter += 1
        if every_n > 1 and (self._save_counter % every_n) != 0:
            return 0
        return self.prune_old_traces(
            max_age_days=max_age_days,
            max_rows_per_session=max_rows_per_session,
        )

    def prune_old_traces(
        self,
        max_age_days: int = 30,
        max_rows_per_session: int = 200,
    ) -> int:
        """Prune traces older than max_age_days and keep at most max_rows_per_session per session.

        Safe and non-blocking: catches exceptions, logs warnings, and returns count of pruned rows.
        """
        try:
            now = time.time()
            cutoff_time = now - (max_age_days * 86400)
            total_deleted = 0
            with self._db.transaction() as conn:
                # 1. Prune traces older than max_age_days
                cur1 = conn.execute(
                    "DELETE FROM agent_conversation_traces WHERE start_time < ?",
                    (cutoff_time,),
                )
                total_deleted += max(0, cur1.rowcount)

                # 2. Prune traces exceeding max_rows_per_session per session using window function
                cur2 = conn.execute(
                    """
                    DELETE FROM agent_conversation_traces
                    WHERE trace_id IN (
                        SELECT trace_id FROM (
                            SELECT trace_id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY COALESCE(session_id, task_id, '')
                                       ORDER BY start_time DESC
                                   ) as rn
                            FROM agent_conversation_traces
                        ) WHERE rn > ?
                    )
                    """,
                    (max_rows_per_session,),
                )
                total_deleted += max(0, cur2.rowcount)
            return total_deleted
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Error pruning conversation traces: %s", exc)
            return 0

    def list_traces(self, limit: int = 50, session_id: str | None = None) -> list[dict[str, Any]]:
        """List summary of traces sorted by most recent."""
        with self._db.read_connection() as conn:
            if session_id:
                cursor = conn.execute(
                    """
                    SELECT trace_id, session_id, run_id, task_id, query, status, start_time,
                           updated_at, total_duration_ms, ttft_ms, ttfa_ms, ttlt_ms, steps_count
                    FROM agent_conversation_traces
                    WHERE session_id = ?
                    ORDER BY start_time DESC LIMIT ?
                    """,
                    (session_id, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT trace_id, session_id, run_id, task_id, query, status, start_time,
                           updated_at, total_duration_ms, ttft_ms, ttfa_ms, ttlt_ms, steps_count
                    FROM agent_conversation_traces
                    ORDER BY start_time DESC LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Get full detailed trace payload by trace_id."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                "SELECT payload FROM agent_conversation_traces WHERE trace_id = ?",
                (trace_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return json.loads(row["payload"])


_global_trace_store: TraceStore | None = None


def get_trace_store() -> TraceStore:
    """Retrieve singleton TraceStore."""
    global _global_trace_store
    if _global_trace_store is None:
        _global_trace_store = TraceStore()
    return _global_trace_store
