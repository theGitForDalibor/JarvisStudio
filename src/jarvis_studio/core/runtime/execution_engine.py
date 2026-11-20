"""ExecutionEngine: In-process asynchronous task execution engine and worker pool.

Provides decoupled task background execution, single-flight enforcement, bounded concurrency,
and execution control (Cancel, Pause, Resume, Append Instruction) for AgentStudio.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis_studio.core.tasks.coordinator import TaskCoordinator, get_task_coordinator
from jarvis_studio.core.tasks.models import (
    AgentRun,
    AgentTask,
    EventType,
    RunStatus,
    RunTriggerType,
    TaskStatus,
)

if TYPE_CHECKING:
    from jarvis_studio.service.agent_service import AgentExecutionResult, AgentService

_logger = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    """An execution job queued for processing by worker pool."""

    task_id: str
    run_id: str
    user_message: str | None = None
    trigger_type: RunTriggerType = RunTriggerType.INITIAL
    checkpoint_id: str | None = None
    fresh_checkpoint: bool = False
    created_at: float = field(default_factory=time.time)
    cancelled: bool = False
    is_pause: bool = False
    future: asyncio.Future[Any] | None = None


class ExecutionEngine:
    """In-process background worker pool and execution manager."""

    def __init__(
        self,
        coordinator: TaskCoordinator | None = None,
        agent_service: AgentService | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self.coordinator = coordinator or get_task_coordinator()
        self._agent_service = agent_service
        self.max_concurrency = max(1, max_concurrency)

        self._started = False
        self._shutdown_event = asyncio.Event()
        self._queue: asyncio.Queue[ExecutionRequest] = asyncio.Queue()
        self._workers: list[asyncio.Task[Any]] = []
        self._running_tasks: dict[str, tuple[asyncio.Task[Any], ExecutionRequest]] = {}
        self._queued_requests: dict[str, ExecutionRequest] = {}
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._started and not self._shutdown_event.is_set()

    @property
    def agent_service(self) -> AgentService:
        if self._agent_service is not None:
            return self._agent_service
        from jarvis_studio.service.agent_service import get_agent_service

        svc = get_agent_service()
        if svc is None:
            raise RuntimeError("AgentService is not available in ExecutionEngine")
        return svc

    def set_agent_service(self, agent_service: AgentService) -> None:
        self._agent_service = agent_service

    async def start(self) -> None:
        """Start worker pool background coroutines."""
        if self._started:
            return
        self._shutdown_event.clear()
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"task-worker-{i}")
            for i in range(self.max_concurrency)
        ]
        self._started = True
        _logger.info("ExecutionEngine started with %d workers", self.max_concurrency)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Gracefully shut down workers and cancel pending or in-flight tasks."""
        if not self._started:
            return
        self._shutdown_event.set()

        # 1. Cancel and drain queued requests
        async with self._lock:
            for task_id, req in list(self._queued_requests.items()):
                req.cancelled = True
                try:
                    self.coordinator.cancel_task_and_run(
                        task_id, req.run_id, error_message="Runtime shutting down"
                    )
                    self.coordinator.record_event(
                        task_id,
                        EventType.TASK_CANCELLED,
                        run_id=req.run_id,
                        payload={"reason": "Runtime shutting down"},
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.debug("Error cancelling queued task %s: %s", task_id, exc)
                if req.future and not req.future.done():
                    req.future.cancel()
            self._queued_requests.clear()

        # 2. Cancel in-flight running handles
        running_handles: list[asyncio.Task[Any]] = []
        async with self._lock:
            for task_id, (handle, req) in list(self._running_tasks.items()):
                if not handle.done():
                    handle.cancel()
                    running_handles.append(handle)

        # 3. Cancel worker pool tasks
        for worker in self._workers:
            if not worker.done():
                worker.cancel()

        all_to_wait = running_handles + self._workers
        if all_to_wait:
            _, pending = await asyncio.wait(all_to_wait, timeout=max(0.1, timeout))
            for p in pending:
                p.cancel()

        self._started = False
        _logger.info("ExecutionEngine shutdown complete")

    async def is_task_active(self, task_id: str) -> bool:
        """Check whether task is actively executing or queued in the engine."""
        async with self._lock:
            return task_id in self._running_tasks or task_id in self._queued_requests

    async def get_running_task_ids(self) -> list[str]:
        async with self._lock:
            return list(self._running_tasks.keys())

    async def get_queued_task_ids(self) -> list[str]:
        async with self._lock:
            return list(self._queued_requests.keys())

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
    ) -> tuple[AgentRun, asyncio.Future[Any] | None]:
        """Submit a task for asynchronous background execution with single-flight check."""
        if not self._started or self._shutdown_event.is_set():
            await self.start()

        # Enforce single-flight constraint in engine memory
        async with self._lock:
            if task_id in self._running_tasks or task_id in self._queued_requests:
                raise ValueError(f"Task '{task_id}' is already executing or queued")

            task = self.coordinator.get_task(task_id)
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task.archived_at is not None:
                raise ValueError(f"Task '{task_id}' is archived and cannot be executed")
            if task.status == TaskStatus.UNKNOWN:
                raise RuntimeError(
                    f"Task '{task_id}' is in UNKNOWN state due to uncertain external side effects "
                    "and cannot be automatically re-executed."
                )
            blocked = [
                TaskStatus.RUNNING,
                TaskStatus.EXECUTING_TOOL,
                TaskStatus.WAITING_APPROVAL,
                TaskStatus.CANCELLING,
                TaskStatus.QUEUED,
            ]
            if trigger_type not in (RunTriggerType.USER_INPUT, RunTriggerType.RESUME):
                blocked.append(TaskStatus.COMPLETED)
            if task.status in blocked:
                raise ValueError(
                    f"Task '{task_id}' cannot be executed while in state '{task.status.value}'"
                )

            hitl = getattr(self.agent_service, "hitl_manager", None)
            runtime_id = (
                getattr(hitl, "runtime_instance_id", "standalone")
                if hitl is not None
                else "standalone"
            ) or "standalone"
            actual_run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"

            run = self.coordinator.queue_run(
                task_id=task_id,
                runtime_id=runtime_id,
                trigger_type=trigger_type,
                run_id=actual_run_id,
                checkpoint_id=checkpoint_id,
            )

            self.coordinator.record_event(
                task_id=task_id,
                event_type=EventType.TASK_QUEUED,
                run_id=run.run_id,
                payload={
                    "run_seq": run.run_seq,
                    "trigger_type": trigger_type.value,
                    "runtime_id": runtime_id,
                    "checkpoint_id": checkpoint_id,
                },
            )

            loop = asyncio.get_running_loop()
            future = loop.create_future() if return_future else None

            req = ExecutionRequest(
                task_id=task_id,
                run_id=run.run_id,
                user_message=user_message,
                trigger_type=trigger_type,
                checkpoint_id=checkpoint_id,
                fresh_checkpoint=fresh_checkpoint,
                future=future,
            )
            self._queued_requests[task_id] = req
            await self._queue.put(req)

        return run, future

    async def cancel_task(self, task_id: str, *, reason: str = "Cancelled by user") -> AgentTask:
        """Cancel a queued or running task immediately."""
        async with self._lock:
            # 1. Check if queued (not yet picked up)
            if task_id in self._queued_requests:
                req = self._queued_requests.pop(task_id)
                req.cancelled = True
                self.coordinator.cancel_task_and_run(task_id, req.run_id, error_message=reason)
                self.coordinator.record_event(
                    task_id=task_id,
                    event_type=EventType.TASK_CANCELLED,
                    run_id=req.run_id,
                    payload={"reason": reason, "stage": "queued"},
                )
                if req.future and not req.future.done():
                    req.future.cancel()
                task = self.coordinator.get_task(task_id)
                return task or self.coordinator.get_task(task_id)  # type: ignore[return-value]

            # 2. Check if currently running in a worker
            if task_id in self._running_tasks:
                handle, req = self._running_tasks[task_id]
                req.is_pause = False
                self.coordinator.mark_cancelling(task_id, req.run_id)
                self.coordinator.record_event(
                    task_id=task_id,
                    event_type=EventType.TASK_CANCELLING,
                    run_id=req.run_id,
                    payload={"reason": reason},
                )
                handle.cancel()
                task = self.coordinator.get_task(task_id)
                return task or self.coordinator.get_task(task_id)  # type: ignore[return-value]

        # 3. Not active in engine: delegate to coordinator
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMEOUT,
        ):
            if task.current_run_id:
                self.coordinator.cancel_task_and_run(task_id, task.current_run_id, error_message=reason)
            else:
                self.coordinator.set_task_status(task_id, TaskStatus.CANCELLED, error_message=reason)
            self.coordinator.record_event(
                task_id=task_id,
                event_type=EventType.TASK_CANCELLED,
                run_id=task.current_run_id,
                payload={"reason": reason},
            )
        return self.coordinator.get_task(task_id) or task

    async def pause_task(self, task_id: str, *, reason: str = "Paused by user") -> AgentTask:
        """Pause a queued or in-flight task."""
        async with self._lock:
            # 1. If queued, pause directly
            if task_id in self._queued_requests:
                req = self._queued_requests.pop(task_id)
                req.cancelled = True
                self.coordinator.pause_task_and_run(task_id, req.run_id)
                self.coordinator.record_event(
                    task_id=task_id,
                    event_type=EventType.TASK_PAUSED,
                    run_id=req.run_id,
                    payload={"reason": reason, "stage": "queued"},
                )
                if req.future and not req.future.done():
                    req.future.cancel()
                return self.coordinator.get_task(task_id)  # type: ignore[return-value]

            # 2. If running, signal cooperative pause (do NOT force-cancel in-flight tools)
            if task_id in self._running_tasks:
                handle, req = self._running_tasks[task_id]
                task = self.coordinator.get_task(task_id)
                if task is not None and task.status == TaskStatus.WAITING_APPROVAL:
                    raise ValueError(
                        f"Task '{task_id}' is waiting for HITL approval and cannot be paused"
                    )
                req.is_pause = True
                return task  # type: ignore[return-value]

        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.status in (TaskStatus.RUNNING, TaskStatus.EXECUTING_TOOL):
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
        raise ValueError(
            f"Task '{task_id}' cannot be paused while in state '{task.status.value}'"
        )

    async def resume_task(self, task_id: str, *, checkpoint_id: str | None = None) -> AgentRun:
        """Resume a paused or interrupted task with a new run inheriting the checkpoint."""
        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.status not in (TaskStatus.PAUSED, TaskStatus.INTERRUPTED):
            raise ValueError(
                f"Task '{task_id}' cannot be resumed from state '{task.status.value}'; "
                "must be PAUSED or INTERRUPTED"
            )

        run, _ = await self.submit_task(
            task_id,
            trigger_type=RunTriggerType.RESUME,
            checkpoint_id=checkpoint_id,
            fresh_checkpoint=False,
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
        """Append an instruction to an existing task and queue an execution run with user input."""
        clean_text = instruction.strip()
        if not clean_text:
            raise ValueError("Instruction cannot be empty")

        task = self.coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")

        self.coordinator.record_event(
            task_id=task_id,
            event_type=EventType.INSTRUCTION_APPENDED,
            payload={"instruction": clean_text},
        )

        run, _ = await self.submit_task(
            task_id,
            user_message=clean_text,
            trigger_type=RunTriggerType.USER_INPUT,
            checkpoint_id=checkpoint_id,
            fresh_checkpoint=False,
        )
        return run

    async def _worker_loop(self, worker_id: int) -> None:
        """Background worker loop picking up tasks from the queue."""
        while not self._shutdown_event.is_set():
            try:
                req = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                if self._shutdown_event.is_set() or req.cancelled:
                    continue

                task_id = req.task_id
                current_async_task = asyncio.current_task()
                assert current_async_task is not None

                async with self._lock:
                    if req.cancelled:
                        continue
                    self._queued_requests.pop(task_id, None)
                    self._running_tasks[task_id] = (current_async_task, req)

                try:
                    result = await self.agent_service.execute_task(
                        task_id,
                        user_message=req.user_message,
                        run_id=req.run_id,
                        trigger_type=req.trigger_type,
                        checkpoint_id=req.checkpoint_id,
                        fresh_checkpoint=req.fresh_checkpoint,
                        allow_queued=True,
                        pause_checker=lambda r=req: r.is_pause,
                    )
                    if req.future and not req.future.done():
                        req.future.set_result(result)
                except asyncio.CancelledError:
                    events = self.coordinator.list_events_for_task(task_id, limit=500)
                    if not any(
                        e.event_type == EventType.TASK_CANCELLED and e.run_id == req.run_id
                        for e in events
                    ):
                        self.coordinator.record_event(
                            task_id=task_id,
                            event_type=EventType.TASK_CANCELLED,
                            run_id=req.run_id,
                            payload={"reason": "Cancelled"},
                        )
                    if req.future and not req.future.done():
                        req.future.cancel()
                except Exception as exc:  # noqa: BLE001
                    _logger.error("Worker %d failed execution for task %s: %s", worker_id, task_id, exc)
                    if req.future and not req.future.done():
                        req.future.set_exception(exc)
                finally:
                    async with self._lock:
                        self._running_tasks.pop(task_id, None)
            finally:
                self._queue.task_done()


_global_execution_engine: ExecutionEngine | None = None


def get_execution_engine() -> ExecutionEngine | None:
    """Return process-wide active ExecutionEngine, if any."""
    global _global_execution_engine
    if _global_execution_engine is not None:
        return _global_execution_engine
    from jarvis_studio.core.runtime.agent_runtime import RuntimeState, get_active_runtime

    rt = get_active_runtime()
    if rt is not None and rt.state == RuntimeState.READY:
        try:
            return rt.execution_engine
        except Exception:
            return None
    return None


def set_execution_engine(engine: ExecutionEngine | None) -> None:
    """Set process-wide ExecutionEngine singleton."""
    global _global_execution_engine
    _global_execution_engine = engine


def reset_global_execution_engine() -> None:
    """Reset global ExecutionEngine singleton."""
    global _global_execution_engine
    _global_execution_engine = None
