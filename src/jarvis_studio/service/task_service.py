"""TaskService: unified domain service for task lifecycle, run management, and deliverable coordination."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from jarvis_studio.config import get_settings
from jarvis_studio.core.tasks.coordinator import TaskCoordinator, get_task_coordinator
from jarvis_studio.core.tasks.models import (
    AgentRun,
    AgentTask,
    DeliverableRecord,
    EventRecord,
    EventType,
    RunStatus,
    RunTriggerType,
    TaskStatus,
)
from jarvis_studio.service.deliverable_manager import (
    DeliverableManager,
    get_deliverable_manager,
)

_logger = logging.getLogger(__name__)


class TaskService:
    """Domain service orchestrating Task, Run, and Deliverable operations."""

    def __init__(
        self,
        coordinator: TaskCoordinator | None = None,
        deliverable_mgr: DeliverableManager | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.coordinator = coordinator or get_task_coordinator(db_path=db_path)
        self.deliverable_mgr = deliverable_mgr or get_deliverable_manager(db_path=db_path)

    def create_task(
        self,
        query: str,
        *,
        session_id: str | None = None,
        assigned_agent_id: str = "general_agent",
        priority: str = "NORMAL",
        initiator: str = "user",
        plan_json: str | None = None,
        auto_start_run: bool = False,
        runtime_id: str | None = None,
    ) -> tuple[AgentTask, AgentRun | None]:
        """Create an independent task (optionally linked to a session), with an optional initial run."""
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Task query cannot be empty")

        task = self.coordinator.create_task(
            session_id=session_id,
            query=clean_query,
            initiator=initiator,
            assigned_agent_id=assigned_agent_id,
            priority=priority,
            plan_json=plan_json,
        )

        self.coordinator.record_event(
            task_id=task.task_id,
            event_type=EventType.TASK_CREATED,
            payload={
                "query": clean_query,
                "session_id": session_id,
                "assigned_agent_id": assigned_agent_id,
                "priority": priority,
            },
        )

        run: AgentRun | None = None
        if auto_start_run:
            rt_id = runtime_id or "default_runtime"
            run = self.start_run(task.task_id, runtime_id=rt_id, trigger_type=RunTriggerType.INITIAL)
            task.current_run_id = run.run_id
            task.status = TaskStatus.RUNNING

        return task, run

    def start_run(
        self,
        task_id: str,
        runtime_id: str,
        *,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        checkpoint_id: str | None = None,
    ) -> AgentRun:
        """Start a new execution run under the given task."""
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")

        run = self.coordinator.start_run(
            task_id=task_id,
            runtime_id=runtime_id,
            trigger_type=trigger_type,
            checkpoint_id=checkpoint_id,
        )

        self.coordinator.record_event(
            task_id=task_id,
            event_type=EventType.RUN_STARTED,
            run_id=run.run_id,
            payload={
                "run_seq": run.run_seq,
                "trigger_type": trigger_type.value,
                "runtime_id": runtime_id,
                "checkpoint_id": checkpoint_id,
            },
        )
        return run

    def get_task(self, task_id: str) -> AgentTask | None:
        """Retrieve a task by ID."""
        return self.coordinator.get_task(task_id)

    def list_tasks(
        self,
        session_id: str | None = None,
        *,
        status: TaskStatus | str | None = None,
        limit: int = 50,
    ) -> list[AgentTask]:
        """List tasks with optional session or status filters."""
        return self.coordinator.list_tasks(session_id=session_id, status=status, limit=limit)

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        """Retrieve complete task detail with runs, deliverables, and recent execution events."""
        task = self.coordinator.get_task(task_id)
        if task is None:
            return None

        runs = self.coordinator.list_runs(task_id)
        deliverables = self.deliverable_mgr.list_deliverables_for_task(task_id)
        events = self.coordinator.list_events_for_task(task_id, since_seq=0, limit=100)

        return {
            "task": task,
            "runs": runs,
            "deliverables": deliverables,
            "events": events,
        }

    def cancel_task(self, task_id: str, *, reason: str = "Cancelled by user") -> AgentTask:
        """Cancel an in-flight or waiting task and its active run."""
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")

        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMEOUT,
        ):
            # Already terminal
            return task

        if task.current_run_id:
            self.coordinator.cancel_task_and_run(
                task_id=task_id,
                run_id=task.current_run_id,
                error_message=reason,
            )
        else:
            self.coordinator.set_task_status(
                task_id=task_id,
                status=TaskStatus.CANCELLED,
                error_message=reason,
            )

        self.coordinator.record_event(
            task_id=task_id,
            event_type=EventType.RUN_FAILED,
            run_id=task.current_run_id,
            payload={"reason": reason, "action": "cancelled"},
        )

        refreshed = self.coordinator.get_task(task_id)
        return refreshed or task

    def delete_task(self, task_id: str) -> bool:
        """Delete a task, purging its physical deliverables and all database relations.

        Refuses deletion if the task is currently RUNNING or EXECUTING_TOOL.
        """
        task = self.coordinator.get_task(task_id)
        if task is None:
            return False

        if task.status in (TaskStatus.RUNNING, TaskStatus.EXECUTING_TOOL):
            raise ValueError(
                f"Task '{task_id}' cannot be deleted while in active state '{task.status.value}'"
            )

        # 1. Clean up physical deliverable files
        self.deliverable_mgr.delete_task_deliverables_dir(task_id)

        # 2. Delete database records
        return self.coordinator.delete_task(task_id)

    def list_events(self, task_id: str, *, since_seq: int = 0, limit: int = 100) -> list[EventRecord]:
        """Fetch observable business events for task after since_seq."""
        return self.coordinator.list_events_for_task(task_id, since_seq=since_seq, limit=limit)


_default_task_service: TaskService | None = None
_default_task_service_lock = threading.Lock()


def get_task_service(db_path: str | Path | None = None) -> TaskService:
    """Return process-wide default TaskService singleton."""
    global _default_task_service
    if _default_task_service is not None and db_path is None:
        return _default_task_service

    with _default_task_service_lock:
        if _default_task_service is not None and db_path is None:
            return _default_task_service
        coord = get_task_coordinator(db_path=db_path)
        deliv = get_deliverable_manager(db_path=db_path)
        svc = TaskService(coordinator=coord, deliverable_mgr=deliv, db_path=db_path)
        if db_path is None:
            _default_task_service = svc
        return svc


def reset_global_task_service() -> None:
    """Reset global TaskService singleton."""
    global _default_task_service
    with _default_task_service_lock:
        _default_task_service = None
