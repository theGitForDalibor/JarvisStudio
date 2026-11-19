"""Domain models for agent tasks and execution runs."""

from __future__ import annotations

import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Lifecycle status of an agent task."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING_TOOL = "EXECUTING_TOOL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    INTERRUPTED = "INTERRUPTED"


class RunStatus(str, Enum):
    """Lifecycle status of an individual execution run."""

    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class RunTriggerType(str, Enum):
    """The trigger reason for starting a run."""

    INITIAL = "initial"
    HITL_RESUME = "hitl_resume"
    RECOVERY_RESUME = "recovery_resume"


class DeliverableType(str, Enum):
    """Supported types of task deliverables."""

    MARKDOWN_REPORT = "markdown_report"
    CODE_PATCH = "code_patch"
    FILE = "file"
    DATA_TABLE = "data_table"
    JSON_ARTIFACT = "json_artifact"


class DeliverableRecord(BaseModel):
    """A durable formal task deliverable."""

    deliverable_id: str = Field(default_factory=lambda: f"deliv_{uuid.uuid4().hex[:12]}")
    task_id: str
    run_id: str
    title: str
    deliverable_type: DeliverableType = DeliverableType.MARKDOWN_REPORT
    file_path: str | None = None
    mime_type: str = "text/markdown; charset=utf-8"
    file_size_bytes: int = 0
    sha256_hash: str | None = None
    summary: str | None = None
    content_preview: str | None = None
    metadata_json: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class EventType(str, Enum):
    """Lifecycle event types for execution observability."""

    TASK_CREATED = "task_created"
    RUN_STARTED = "run_started"
    PLAN_UPDATED = "plan_updated"
    AGENT_DELEGATED = "agent_delegated"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    RUN_FAILED = "run_failed"
    DELIVERABLE_CREATED = "deliverable_created"
    TASK_COMPLETED = "task_completed"


class EventRecord(BaseModel):
    """A discrete execution fact emitted during task/run progression."""

    event_id: str = Field(default_factory=lambda: f"ev_{uuid.uuid4().hex[:12]}")
    task_id: str
    run_id: str | None = None
    seq: int
    event_type: EventType
    timestamp: float = Field(default_factory=time.time)
    payload_json: str = "{}"

    @property
    def created_at(self) -> float:
        return self.timestamp

    @property
    def payload(self) -> dict[str, object]:
        try:
            return json.loads(self.payload_json) if self.payload_json else {}
        except Exception:
            return {}


class AgentTask(BaseModel):
    """A durable conversational or tool execution task."""

    task_id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")
    session_id: str | None = None
    thread_id: str
    status: TaskStatus = TaskStatus.CREATED
    query: str
    initiator: str = "user"
    assigned_agent_id: str = "general_agent"
    current_run_id: str | None = None
    plan_json: str | None = None
    priority: str = "NORMAL"
    result_summary: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
    runtime_id: str | None = None
    error_message: str | None = None


class AgentRun(BaseModel):
    """An execution run under an agent task."""

    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    task_id: str
    run_seq: int
    trigger_type: RunTriggerType = RunTriggerType.INITIAL
    status: RunStatus = RunStatus.RUNNING
    runtime_id: str
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    duration_ms: int | None = None
    checkpoint_id: str | None = None
    error_message: str | None = None
