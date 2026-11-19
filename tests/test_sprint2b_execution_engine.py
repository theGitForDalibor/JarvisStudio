"""Tests for Sprint 2B ExecutionEngine, Concurrency, and User Controls."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.runtime.execution_engine import ExecutionEngine
from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import (
    EventType,
    RunStatus,
    RunTriggerType,
    TaskStatus,
)
from jarvis_studio.service.agent_service import AgentExecutionResult, AgentService
from jarvis_studio.service.task_service import TaskService


class _SlowFakeModel:
    """Mock LLM model that can simulate slow processing or cooperative cancellation."""

    def __init__(self, delay: float = 0.05, content: str = "Mock completed response") -> None:
        self.delay = delay
        self.content = content

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return AIMessage(content=self.content)


@pytest.fixture
def base_test_env(tmp_path: Path):
    """Isolated test environment with DB and basic coordinator/task_service."""
    db_path = tmp_path / "engine_test.db"
    coordinator = TaskCoordinator(db_path=db_path)
    task_svc = TaskService(coordinator=coordinator, db_path=db_path)
    return {
        "db_path": db_path,
        "coordinator": coordinator,
        "task_svc": task_svc,
    }


@pytest.mark.asyncio
async def test_execution_engine_single_flight_enforcement(tmp_path: Path):
    """同一 Task 不允许发生非法并发执行 (Single-flight per task)."""
    db_path = tmp_path / "single_flight.db"
    coordinator = TaskCoordinator(db_path=db_path)

    # Create dummy slow agent service
    class SlowAgentService:
        async def execute_task(self, task_id, **kwargs):
            await asyncio.sleep(0.3)
            coordinator.complete_task_and_run(task_id, kwargs.get("run_id", "run_1"))
            return AgentExecutionResult(thread_id=task_id, run_id="run_1", task_id=task_id, status="completed")

    engine = ExecutionEngine(coordinator=coordinator, agent_service=SlowAgentService(), max_concurrency=2)
    await engine.start()
    try:
        t1 = coordinator.create_task(query="Task 1", thread_id="th_1")

        # First submission succeeds
        run1, fut1 = await engine.submit_task(t1.task_id, return_future=True)
        assert run1.task_id == t1.task_id

        # Duplicate submission while running must fail immediately
        with pytest.raises(ValueError, match="already executing or queued"):
            await engine.submit_task(t1.task_id)

        # Wait for completion
        assert fut1 is not None
        await fut1

        # After completion, task status is COMPLETED; re-executing without fresh run or resume is blocked
        with pytest.raises(ValueError, match="cannot be executed while in state 'COMPLETED'"):
            # Direct execute on completed task without resume
            task_fresh = coordinator.get_task(t1.task_id)
            assert task_fresh.status == TaskStatus.COMPLETED
            await engine.submit_task(t1.task_id)
    finally:
        await engine.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_concurrency_bounded_worker_pool(tmp_path: Path):
    """不同 Task 能够按照资源限制并行执行 (Bounded concurrency)."""
    db_path = tmp_path / "bounded.db"
    coordinator = TaskCoordinator(db_path=db_path)

    active_executions = 0
    max_observed_concurrency = 0
    lock = asyncio.Lock()

    class MonitoredSlowService:
        async def execute_task(self, task_id, **kwargs):
            nonlocal active_executions, max_observed_concurrency
            async with lock:
                active_executions += 1
                if active_executions > max_observed_concurrency:
                    max_observed_concurrency = active_executions

            await asyncio.sleep(0.15)

            async with lock:
                active_executions -= 1
            coordinator.complete_task_and_run(task_id, kwargs.get("run_id", "run_x"))
            return AgentExecutionResult(thread_id=task_id, run_id="run_x", task_id=task_id, status="completed")

    # Pool with strictly 2 concurrent workers
    engine = ExecutionEngine(coordinator=coordinator, agent_service=MonitoredSlowService(), max_concurrency=2)
    await engine.start()
    try:
        tasks = [coordinator.create_task(query=f"Task {i}", thread_id=f"th_{i}") for i in range(5)]
        futures = []

        # Submit all 5 tasks rapidly
        for t in tasks:
            _, fut = await engine.submit_task(t.task_id, return_future=True)
            futures.append(fut)

        # Wait for all to finish
        results = await asyncio.gather(*futures)
        assert len(results) == 5

        # Verify max concurrent executions never exceeded 2
        assert max_observed_concurrency == 2

        # Verify all 5 tasks reached COMPLETED
        for t in tasks:
            refreshed = coordinator.get_task(t.task_id)
            assert refreshed.status == TaskStatus.COMPLETED
    finally:
        await engine.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_real_inflight_cancellation(tmp_path: Path):
    """真正取消正在执行的协程任务，而不是只改数据库状态。"""
    db_path = tmp_path / "cancel.db"
    coordinator = TaskCoordinator(db_path=db_path)

    was_cancelled = False

    class LongRunningService:
        async def execute_task(self, task_id, **kwargs):
            nonlocal was_cancelled
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                was_cancelled = True
                coordinator.cancel_task_and_run(task_id, kwargs.get("run_id", "run_x"), error_message="Cancelled")
                raise

    engine = ExecutionEngine(coordinator=coordinator, agent_service=LongRunningService(), max_concurrency=2)
    await engine.start()
    try:
        t = coordinator.create_task(query="Cancel me", thread_id="th_cancel")
        run, fut = await engine.submit_task(t.task_id, return_future=True)

        # Allow worker to pick up task and start sleeping
        await asyncio.sleep(0.05)

        # Verify it is actively running
        assert await engine.is_task_active(t.task_id)

        # Cancel the task
        cancelled_task = await engine.cancel_task(t.task_id, reason="User requested cancel")
        assert cancelled_task.status in (TaskStatus.CANCELLING, TaskStatus.CANCELLED)

        # Wait briefly for coroutine cancellation to process
        await asyncio.sleep(0.05)

        assert was_cancelled is True
        final_task = coordinator.get_task(t.task_id)
        assert final_task.status == TaskStatus.CANCELLED

        # Verify event was emitted
        events = coordinator.list_events_for_task(t.task_id)
        event_types = [e.event_type for e in events]
        assert EventType.TASK_CANCELLING in event_types
        assert EventType.TASK_CANCELLED in event_types
    finally:
        await engine.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_pause_and_resume_lifecycle(tmp_path: Path):
    """Pause an active task and resume it with a follow-up run inheriting state."""
    db_path = tmp_path / "pause_resume.db"
    coordinator = TaskCoordinator(db_path=db_path)

    class PausableService:
        def __init__(self):
            self.iteration = 0

        async def execute_task(self, task_id, **kwargs):
            self.iteration += 1
            if self.iteration == 1:
                # First run will be paused while sleeping
                await asyncio.sleep(5.0)
            else:
                # Resumed run completes quickly
                await asyncio.sleep(0.05)
                coordinator.complete_task_and_run(task_id, kwargs.get("run_id", "run_2"))
                return AgentExecutionResult(thread_id=task_id, run_id="run_2", task_id=task_id, status="completed")

    svc = PausableService()
    engine = ExecutionEngine(coordinator=coordinator, agent_service=svc, max_concurrency=2)
    await engine.start()
    try:
        t = coordinator.create_task(query="Pausable task", thread_id="th_pause")
        run1, _ = await engine.submit_task(t.task_id)

        # Let worker start
        await asyncio.sleep(0.05)

        # Pause the running task
        paused_task = await engine.pause_task(t.task_id)
        await asyncio.sleep(0.05)

        refreshed = coordinator.get_task(t.task_id)
        assert refreshed.status == TaskStatus.PAUSED

        # Verify TASK_PAUSED event
        events = coordinator.list_events_for_task(t.task_id)
        assert any(e.event_type == EventType.TASK_PAUSED for e in events)

        # Resume the task
        run2 = await engine.resume_task(t.task_id)
        assert run2.trigger_type == RunTriggerType.RESUME
        assert run2.run_seq == 2

        # Allow resumed run to complete
        await asyncio.sleep(0.15)

        final_task = coordinator.get_task(t.task_id)
        assert final_task.status == TaskStatus.COMPLETED

        runs = coordinator.list_runs(t.task_id)
        assert len(runs) == 2
        assert runs[0].status == RunStatus.PAUSED
        assert runs[1].status == RunStatus.COMPLETED
    finally:
        await engine.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_append_instruction_flow(tmp_path: Path):
    """Append instruction continues conversation history as a new USER_INPUT run."""
    db_path = tmp_path / "append.db"
    coordinator = TaskCoordinator(db_path=db_path)

    recorded_messages = []

    class AppendService:
        async def execute_task(self, task_id, **kwargs):
            user_msg = kwargs.get("user_message")
            recorded_messages.append(user_msg)
            coordinator.complete_task_and_run(task_id, kwargs.get("run_id", "run_x"))
            return AgentExecutionResult(thread_id=task_id, run_id="run_x", task_id=task_id, status="completed")

    engine = ExecutionEngine(coordinator=coordinator, agent_service=AppendService(), max_concurrency=2)
    await engine.start()
    try:
        t = coordinator.create_task(query="Step 1", thread_id="th_app")
        run1, fut1 = await engine.submit_task(t.task_id, return_future=True)
        await fut1
        assert coordinator.get_task(t.task_id).status == TaskStatus.COMPLETED

        # Now append follow-up instruction
        run2 = await engine.append_instruction(t.task_id, "Please also do Step 2")
        assert run2.trigger_type == RunTriggerType.USER_INPUT
        assert run2.run_seq == 2

        # Allow execution
        await asyncio.sleep(0.1)

        assert coordinator.get_task(t.task_id).status == TaskStatus.COMPLETED
        assert recorded_messages[-1] == "Please also do Step 2"

        events = coordinator.list_events_for_task(t.task_id)
        assert any(e.event_type == EventType.INSTRUCTION_APPENDED for e in events)
    finally:
        await engine.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_cancellation_preserves_indeterminate_tool_outcome_as_unknown(tmp_path: Path):
    """Respect Sprint 1C invariants: in-flight indeterminate tool execution converges to UNKNOWN on cancel."""
    from jarvis_studio.agent.hitl.manager import HITLManager
    from jarvis_studio.agent.hitl.store import HITLStore

    db_path = tmp_path / "unknown_cancel.db"
    coordinator = TaskCoordinator(db_path=db_path)
    hitl_mgr = HITLManager(db_path=db_path, runtime_instance_id="test_rt")
    hitl_store: HITLStore = hitl_mgr.store

    task = coordinator.create_task(query="Dangerous command", thread_id="th_danger")
    run = coordinator.start_run(task.task_id, runtime_id="test_rt")

    # Simulate tool outcome seeded as NOT_STARTED (or approval EXECUTING)
    hitl_store.register_safe_outcome_start(
        thread_id=task.thread_id,
        run_id=run.run_id,
        tool_call_id="call_999",
        tool_name="remote_action",
    )

    # Cancel task coroutine using agent_service pattern
    unfinalized = hitl_store.mark_run_in_flight_outcomes_unknown(run.run_id)
    assert unfinalized == 1

    coordinator.cancel_task_and_run(
        task.task_id,
        run.run_id,
        is_unknown=True,
        error_message="Cancelled while tool was executing",
    )

    task_fresh = coordinator.get_task(task.task_id)
    run_fresh = coordinator.get_run(run.run_id)

    # Both must be UNKNOWN, never CANCELLED or COMPLETED
    assert task_fresh.status == TaskStatus.UNKNOWN
    assert run_fresh.status == RunStatus.UNKNOWN

    # Re-executing an UNKNOWN task must be blocked fail-closed
    with pytest.raises(RuntimeError, match="in UNKNOWN state due to uncertain external side effects"):
        svc = AgentService(
            registry=None,
            checkpointer=None,
            session_service=None,
            hitl_manager=hitl_mgr,
            task_coordinator=coordinator,
        )
        svc._validate_task_executable(task.task_id)
