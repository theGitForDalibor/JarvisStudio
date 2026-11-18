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


class AgentTask(BaseModel):
    """A durable conversational or tool execution task."""

    task_id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")
    session_id: str
    thread_id: str
    status: TaskStatus = TaskStatus.CREATED
    query: str
    initiator: str = "user"
    current_run_id: str | None = None
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
