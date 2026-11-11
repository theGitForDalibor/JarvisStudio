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


@dataclass
class ConversationTrace:
    """Full 5-stage business lifecycle trace for a single agent turn."""

    trace_id: str = field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:12]}")
    session_id: str = ""
    run_id: str = ""
    query: str = ""
    start_time: float = field(default_factory=time.time)

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

    def add_step(
        self,
        step_type: str,
        name: str,
        arguments: Any = None,
        result: Any = None,
        duration_ms: int = 0,
        status: str = "success",
        error: str | None = None,
    ) -> ThinkingStep:
        """Append a thinking step to the trace."""
        step = ThinkingStep(
            seq=len(self.thinking_steps) + 1,
            step_type=step_type,
            name=name,
            arguments=arguments,
            result=result,
            duration_ms=duration_ms,
            status=status,
            error=error,
        )
        self.thinking_steps.append(step)
        return step

    def to_dict(self) -> dict[str, Any]:
        """Convert trace to JSON-serializable dictionary."""
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "query": self.query,
            "start_time": self.start_time,
            "status": self.status,
            "input_stage": self.input_stage,
            "guard_stage": self.guard_stage,
            "preload_stage": self.preload_stage,
            "thinking_steps": [asdict(s) for s in self.thinking_steps],
            "output_text": self.output_text,
            "error": self.error,
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
        self.db_path = self._db.db_path
        if not self._db.is_memory:
            initialize_sqlite_database(
                self.db_path, enable_wal=True, enable_foreign_keys=True
            )
            run_app_migrations(self.db_path)
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
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_time REAL NOT NULL,
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

    def save_trace(self, trace: ConversationTrace) -> None:
        """Persist completed trace into SQLite."""
        payload = json.dumps(trace.to_dict(), ensure_ascii=False)
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_conversation_traces (
                    trace_id, session_id, run_id, query, status,
                    start_time, total_duration_ms, ttft_ms, ttfa_ms, ttlt_ms,
                    steps_count, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

    def list_traces(self, limit: int = 50, session_id: str | None = None) -> list[dict[str, Any]]:
        """List summary of traces sorted by most recent."""
        with self._db.read_connection() as conn:
            if session_id:
                cursor = conn.execute(
                    """
                    SELECT trace_id, session_id, run_id, query, status, start_time,
                           total_duration_ms, ttft_ms, ttfa_ms, ttlt_ms, steps_count
                    FROM agent_conversation_traces
                    WHERE session_id = ?
                    ORDER BY start_time DESC LIMIT ?
                    """,
                    (session_id, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT trace_id, session_id, run_id, query, status, start_time,
                           total_duration_ms, ttft_ms, ttfa_ms, ttlt_ms, steps_count
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
