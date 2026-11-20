"""TaskService: unified domain service for task lifecycle, run management, and deliverable coordination."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis_studio.agent.hitl.models import ApprovalDecision
    from jarvis_studio.service.agent_service import AgentExecutionResult, AgentService

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
        execution_engine: ExecutionEngine | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.coordinator = coordinator or get_task_coordinator(db_path=db_path)
        self.deliverable_mgr = deliverable_mgr or get_deliverable_manager(db_path=db_path)
        self._execution_engine = execution_engine

    @property
    def execution_engine(self) -> ExecutionEngine | None:
        """Resolved active ExecutionEngine instance, if available."""
        if self._execution_engine is not None:
            return self._execution_engine
        from jarvis_studio.core.runtime.execution_engine import get_execution_engine

        return get_execution_engine()

    def create_task(
        self,
        query: str,
        *,
        session_id: str | None = None,
        assigned_agent_id: str = "general_agent",
        priority: str = "NORMAL",
        initiator: str = "user",
        plan_json: str | None = None,
        knowledge_scope: dict[str, Any] | Any | None = None,
        auto_start_run: bool = False,
        runtime_id: str | None = None,
    ) -> tuple[AgentTask, AgentRun | None]:
        """Create an independent task (optionally linked to a session), with an optional initial run."""
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Task query cannot be empty")

        scope_json: str | None = None
        if knowledge_scope is not None:
            import json
            from jarvis_studio.core.tasks.models import KnowledgeScope

            if isinstance(knowledge_scope, KnowledgeScope):
                scope_obj = knowledge_scope
            elif isinstance(knowledge_scope, dict):
                scope_obj = KnowledgeScope(**knowledge_scope)
            else:
                raise ValueError("Invalid knowledge_scope format")
            # Persist even an explicit empty scope so it enforces deny-all instead of NULL (unrestricted)
            scope_json = json.dumps(scope_obj.model_dump(), ensure_ascii=False)

        task = self.coordinator.create_task(
            session_id=session_id,
            query=clean_query,
            initiator=initiator,
            assigned_agent_id=assigned_agent_id,
            priority=priority,
            plan_json=plan_json,
            knowledge_scope_json=scope_json,
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
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[AgentTask]:
        """List tasks with optional session, status, or archive filters."""
        return self.coordinator.list_tasks(
            session_id=session_id,
            status=status,
            include_archived=include_archived,
            limit=limit,
        )

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

        # Check in-process execution engine for active handle
        engine = self.execution_engine
        if engine is not None:
            if task_id in engine._queued_requests:
                req = engine._queued_requests.pop(task_id, None)
                if req is not None:
                    req.cancelled = True
                    if req.future and not req.future.done():
                        req.future.cancel()
            elif task_id in engine._running_tasks:
                handle, req = engine._running_tasks[task_id]
                self.coordinator.mark_cancelling(task_id, req.run_id)
                self.coordinator.record_event(
                    task_id=task_id,
                    event_type=EventType.TASK_CANCELLING,
                    run_id=req.run_id,
                    payload={"reason": reason},
                )
                handle.cancel()
                refreshed = self.coordinator.get_task(task_id)
                return refreshed or task

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

    def archive_task(self, task_id: str) -> AgentTask | None:
        """Soft-delete (archive) a task while preserving all Runs, Events, and Deliverables."""
        task = self.coordinator.get_task(task_id)
        if task is None:
            return None

        if task.status in (
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.EXECUTING_TOOL,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLING,
        ):
            raise ValueError(
                f"Task '{task_id}' cannot be archived while in active state '{task.status.value}'"
            )

        already_archived = task.archived_at is not None
        archived = self.coordinator.archive_task(task_id)
        if archived is None:
            return None

        if not already_archived:
            self.coordinator.record_event(
                task_id=task_id,
                event_type=EventType.TASK_ARCHIVED,
                run_id=task.current_run_id,
                payload={"archived_at": archived.archived_at},
            )
        return archived

    def delete_task(self, task_id: str) -> bool:
        """Soft-delete (archive) a task without removing its Run or Deliverable history."""
        return self.archive_task(task_id) is not None

    def purge_task(self, task_id: str, *, confirm: bool = False) -> bool:
        """Permanently purge a task, its DB records, and its physical deliverables.

        Requires explicit `confirm=True`. Uses two-phase directory staging inside the SQLite
        transaction so filesystem failures roll back DB deletions and restore disk state.
        """
        if not confirm:
            raise ValueError("Permanent task purge requires explicit confirm=True")

        task = self.coordinator.get_task(task_id)
        if task is None:
            return False

        if task.status in (
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.EXECUTING_TOOL,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLING,
        ):
            raise ValueError(
                f"Task '{task_id}' cannot be purged while in active state '{task.status.value}'"
            )

        staged_dir = self.deliverable_mgr.stage_task_deliverables_dir(task_id)
        target_thread_id = task.thread_id

        try:
            purged = self.coordinator.purge_task(task_id)
        except BaseException:
            if staged_dir is not None and staged_dir.exists():
                try:
                    self.deliverable_mgr.restore_staged_dir(staged_dir, task_id)
                except Exception as restore_exc:  # noqa: BLE001
                    _logger.error(
                        "Failed to restore staged deliverables %s for task %s after purge error: %s",
                        staged_dir,
                        task_id,
                        restore_exc,
                    )
            raise

        # Phase 3: Post-commit physical cleanup of staged directory
        if purged and staged_dir is not None and staged_dir.exists():
            try:
                self.deliverable_mgr.remove_staged_dir(staged_dir)
            except Exception as rm_exc:  # noqa: BLE001
                _logger.warning(
                    "Post-commit cleanup of staged deliverables %s failed (deferred to reconcile): %s",
                    staged_dir,
                    rm_exc,
                )

        # Phase 4: Purge LangGraph checkpoints associated with this task's thread
        if purged and target_thread_id:
            try:
                from jarvis_studio.core.persistence.checkpoint_gc import purge_task_checkpoint_sync
                from jarvis_studio.core.runtime import get_active_runtime

                active_rt = get_active_runtime()
                checkpointer = None
                if active_rt is not None:
                    try:
                        checkpointer = active_rt.checkpointer
                    except Exception:  # noqa: BLE001
                        pass
                purge_task_checkpoint_sync(
                    target_thread_id,
                    db_path=self.coordinator.db_path,
                    checkpointer=checkpointer,
                )
            except Exception as gc_exc:  # noqa: BLE001
                _logger.warning(
                    "Checkpoint purge for task %s (thread %s) failed: %s",
                    task_id,
                    target_thread_id,
                    gc_exc,
                )

        return purged

    def list_events(self, task_id: str, *, since_seq: int = 0, limit: int = 100) -> list[EventRecord]:
        """Fetch observable business events for task after since_seq."""
        return self.coordinator.list_events_for_task(task_id, since_seq=since_seq, limit=limit)

    async def execute_task(
        self,
        task_id: str,
        *,
        agent_service: AgentService | None = None,
        user_message: str | None = None,
        run_id: str | None = None,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        checkpoint_id: str | None = None,
        fresh_checkpoint: bool = False,
    ) -> AgentExecutionResult:
        """Execute a Task directly through AgentService / AgentRuntime."""
        from jarvis_studio.service.agent_service import get_agent_service

        svc = agent_service or get_agent_service()
        return await svc.execute_task(
            task_id,
            user_message=user_message,
            run_id=run_id,
            trigger_type=trigger_type,
            checkpoint_id=checkpoint_id,
            fresh_checkpoint=fresh_checkpoint,
        )

    async def resume_task_approval(
        self,
        task_id: str,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        agent_service: AgentService | None = None,
    ) -> AgentExecutionResult:
        """Resume an interrupted Task approval through AgentService / AgentRuntime."""
        from jarvis_studio.service.agent_service import get_agent_service

        svc = agent_service or get_agent_service()
        return await svc.resume_task_approval(
            task_id,
            approval_id=approval_id,
            decision=decision,
        )

    async def cancel_task_async(
        self, task_id: str, *, reason: str = "Cancelled by user"
    ) -> AgentTask:
        """Cancel a queued or running task with in-flight coroutine cancellation."""
        engine = self.execution_engine
        if engine is not None:
            return await engine.cancel_task(task_id, reason=reason)
        return self.cancel_task(task_id, reason=reason)

    async def pause_task(
        self, task_id: str, *, reason: str = "Paused by user"
    ) -> AgentTask:
        """Pause a queued or running task."""
        engine = self.execution_engine
        if engine is not None:
            return await engine.pause_task(task_id, reason=reason)
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.current_run_id:
            self.coordinator.pause_task_and_run(task_id, task.current_run_id)
        else:
            self.coordinator.set_task_status(task_id, TaskStatus.PAUSED)
        self.coordinator.record_event(
            task_id=task_id,
            event_type=EventType.TASK_PAUSED,
            run_id=task.current_run_id,
            payload={"reason": reason},
        )
        return self.coordinator.get_task(task_id) or task

    async def resume_task(
        self, task_id: str, *, checkpoint_id: str | None = None
    ) -> AgentRun:
        """Resume a paused or interrupted task with a new run inheriting checkpoint."""
        engine = self.execution_engine
        if engine is not None:
            return await engine.resume_task(task_id, checkpoint_id=checkpoint_id)
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.status not in (TaskStatus.PAUSED, TaskStatus.INTERRUPTED):
            raise ValueError(
                f"Task '{task_id}' cannot be resumed from state '{task.status.value}'; "
                "must be PAUSED or INTERRUPTED"
            )
        run = self.start_run(
            task_id,
            runtime_id="standalone",
            trigger_type=RunTriggerType.RESUME,
            checkpoint_id=checkpoint_id,
        )
        self.coordinator.record_event(
            task_id=task_id,
            event_type=EventType.TASK_RESUMED,
            run_id=run.run_id,
            payload={"checkpoint_id": checkpoint_id},
        )
        return run

    async def append_instruction(
        self,
        task_id: str,
        instruction: str,
        *,
        checkpoint_id: str | None = None,
    ) -> AgentRun:
        """Append user instruction to an existing task and queue a new run."""
        engine = self.execution_engine
        if engine is not None:
            return await engine.append_instruction(
                task_id, instruction, checkpoint_id=checkpoint_id
            )
        clean = instruction.strip()
        if not clean:
            raise ValueError("Instruction cannot be empty")
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        self.coordinator.record_event(
            task_id=task_id,
            event_type=EventType.INSTRUCTION_APPENDED,
            payload={"instruction": clean},
        )
        return self.start_run(
            task_id,
            runtime_id="standalone",
            trigger_type=RunTriggerType.USER_INPUT,
            checkpoint_id=checkpoint_id,
        )

    async def submit_task(
        self,
        task_id: str,
        *,
        user_message: str | None = None,
        run_id: str | None = None,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        checkpoint_id: str | None = None,
        fresh_checkpoint: bool = False,
        return_future: bool = False,
    ) -> tuple[AgentRun, Any]:
        """Submit task to ExecutionEngine for background execution."""
        engine = self.execution_engine
        if engine is not None:
            return await engine.submit_task(
                task_id,
                user_message=user_message,
                run_id=run_id,
                trigger_type=trigger_type,
                checkpoint_id=checkpoint_id,
                fresh_checkpoint=fresh_checkpoint,
                return_future=return_future,
            )
        # Fallback if engine is not running: synchronous run
        run = self.start_run(
            task_id,
            runtime_id="standalone",
            trigger_type=trigger_type,
            checkpoint_id=checkpoint_id,
        )
        return run, None


_default_task_service: TaskService | None = None
_default_task_service_lock = threading.Lock()


def get_task_service(db_path: str | Path | None = None) -> TaskService:
    """Return process-wide default TaskService singleton or active runtime TaskService."""
    global _default_task_service
    if db_path is None:
        from jarvis_studio.core.runtime.agent_runtime import RuntimeState, get_active_runtime

        active_rt = get_active_runtime()
        if active_rt is not None and active_rt.state == RuntimeState.READY:
            if (
                _default_task_service is not None
                and _default_task_service.coordinator.db_path == active_rt.task_coordinator.db_path
            ):
                return _default_task_service
            coord = active_rt.task_coordinator
            deliv = get_deliverable_manager(db_path=coord.db_path)
            svc = TaskService(
                coordinator=coord,
                deliverable_mgr=deliv,
                execution_engine=getattr(active_rt, "execution_engine", None),
                db_path=coord.db_path,
            )
            _default_task_service = svc
            return svc

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
