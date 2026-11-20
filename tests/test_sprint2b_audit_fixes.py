"""Audit verification, concurrency, and fault-injection tests for Sprint 2B Final Architecture Audit."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.api.routes.tasks import router as tasks_router
from jarvis_studio.config.models import StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.observability.event_broadcaster import get_event_broadcaster
from jarvis_studio.core.persistence.checkpoint_gc import clean_dangling_checkpoints
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import EventType, RunStatus, RunTriggerType, TaskStatus
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.service.deliverable_manager import DeliverableManager
from jarvis_studio.service.task_service import TaskService
from jarvis_studio.tools.registry import ToolRegistry


class _SequentialToolMockModel(BaseChatModel):
    """Deterministic multi-turn chat model that sequences through scripted AIMessages."""

    responses: list[AIMessage]
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "mock_sequential"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _SequentialToolMockModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])


def _make_settings(tmp_path: Path, db_name: str) -> Settings:
    return Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(tmp_path / db_name),
            workspace_root=str(tmp_path / "workspace"),
        )
    )


@pytest.mark.asyncio
async def test_real_langgraph_multistep_pause_and_resume(tmp_path: Path) -> None:
    """Verify cooperative Pause stops before Step 2 and Resume continues from Checkpoint without re-running Step 1."""
    cfg = _make_settings(tmp_path, "pause_resume_graph.db")
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    try:
        coord = rt.task_coordinator
        engine = rt.execution_engine

        step1_calls = 0
        step2_calls = 0
        step1_started = asyncio.Event()
        allow_step1_finish = asyncio.Event()

        @tool
        async def step_one_tool(x: str) -> str:
            """Execute first step tool."""
            nonlocal step1_calls
            step1_calls += 1
            step1_started.set()
            await allow_step1_finish.wait()
            return f"step1_done:{x}"

        @tool
        async def step_two_tool(y: str) -> str:
            """Execute second step tool."""
            nonlocal step2_calls
            step2_calls += 1
            return f"step2_done:{y}"

        reg = ToolRegistry()
        reg.register(step_one_tool)
        reg.register(step_two_tool)

        mock_llm = _SequentialToolMockModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_s1", "name": "step_one_tool", "args": {"x": "alpha"}}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_s2", "name": "step_two_tool", "args": {"y": "beta"}}],
                ),
                AIMessage(content="Both steps completed!"),
            ]
        )
        graph = build_agent_graph(
            model=mock_llm,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )
        svc = AgentService(
            graph=graph,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_manager=rt.hitl_manager,
            task_coordinator=coord,
            session_service=rt.agent_service.session_service,
        )
        engine.set_agent_service(svc)

        task = coord.create_task(query="Run two tools sequentially")
        run1, fut1 = await engine.submit_task(task.task_id, return_future=True)
        assert fut1 is not None

        # Wait until step_one_tool is actively executing
        await asyncio.wait_for(step1_started.wait(), timeout=10.0)

        # Request cooperative pause while step_one_tool is mid-flight
        await engine.pause_task(task.task_id, reason="Pause before step 2")

        # Let step_one_tool complete cleanly
        allow_step1_finish.set()
        res1 = await asyncio.wait_for(fut1, timeout=10.0)
        assert res1.status == "paused"

        # Verify step 1 ran once, step 2 NEVER ran yet, and NO TASK_CANCELLED was emitted
        assert step1_calls == 1
        assert step2_calls == 0

        paused_task = coord.get_task(task.task_id)
        assert paused_task is not None
        assert paused_task.status == TaskStatus.PAUSED

        events1 = coord.list_events_for_task(task.task_id)
        ev_types1 = [e.event_type for e in events1]
        assert EventType.TASK_PAUSED in ev_types1
        assert EventType.TASK_CANCELLED not in ev_types1

        # Now resume the task from the saved checkpoint
        run2 = await engine.resume_task(task.task_id)
        assert run2.trigger_type == RunTriggerType.RESUME

        # Wait for Run 2 to finish
        for _ in range(50):
            t_now = coord.get_task(task.task_id)
            if t_now and t_now.status == TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.05)

        final_task = coord.get_task(task.task_id)
        assert final_task is not None
        assert final_task.status == TaskStatus.COMPLETED

        # Step 1 MUST NOT have re-executed; Step 2 MUST have executed once!
        assert step1_calls == 1
        assert step2_calls == 1

        runs = coord.list_runs(task.task_id)
        assert len(runs) == 2
        assert runs[0].status == RunStatus.PAUSED
        assert runs[1].status == RunStatus.COMPLETED
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_pause_rejects_invalid_and_waiting_approval_states(tmp_path: Path) -> None:
    """Verify pause_task rejects COMPLETED, UNKNOWN, and WAITING_APPROVAL tasks."""
    db_path = tmp_path / "pause_guards.db"
    coord = TaskCoordinator(db_path=db_path)
    engine = AgentRuntime(_make_settings(tmp_path, "pause_guards.db"))._execution_engine
    # Standalone engine over coord
    from jarvis_studio.core.runtime.execution_engine import ExecutionEngine

    eng = ExecutionEngine(coordinator=coord, max_concurrency=1)
    await eng.start()
    try:
        t_wait = coord.create_task(query="Waiting approval")
        run_w = coord.start_run(t_wait.task_id, runtime_id="test")
        coord.pause_for_approval(t_wait.task_id, run_w.run_id)
        with pytest.raises(ValueError, match="cannot be paused"):
            await eng.pause_task(t_wait.task_id)

        t_done = coord.create_task(query="Done")
        run_d = coord.start_run(t_done.task_id, runtime_id="test")
        coord.complete_task_and_run(t_done.task_id, run_d.run_id)
        with pytest.raises(ValueError, match="cannot be paused"):
            await eng.pause_task(t_done.task_id)
    finally:
        await eng.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_sse_replay_live_handoff_concurrent_events_no_gap_no_dup(tmp_path: Path) -> None:
    """Stress-test SSE Replay/Live handoff under concurrent event emission and out-of-order broadcast."""
    cfg = _make_settings(tmp_path, "sse_handoff_race.db")
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/v1")

    try:
        coord = rt.task_coordinator
        task = coord.create_task(query="SSE Handoff Stress Task")
        run = coord.start_run(task.task_id, runtime_id=rt.instance_id)

        # Seed initial 5 historical events
        for i in range(1, 6):
            coord.record_event(
                task.task_id, EventType.PLAN_UPDATED, run_id=run.run_id, payload={"idx": i}
            )

        transport = httpx.ASGITransport(app=app)
        received_seqs: list[int] = []

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

            async def consume_sse() -> None:
                async with client.stream(
                    "GET", f"/api/v1/tasks/{task.task_id}/events/stream?since_seq=0"
                ) as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if line.startswith("id:"):
                            received_seqs.append(int(line.split(":", 1)[1].strip()))
                        elif line.startswith("event:") and "task_completed" in line:
                            break

            async def emit_concurrent_events() -> None:
                # Wait briefly for SSE handler to subscribe and start replaying 1..5
                await asyncio.sleep(0.05)

                # Emit events 6..24 concurrently from multiple async tasks & worker threads
                async def _emit_one(idx: int) -> None:
                    await asyncio.to_thread(
                        coord.record_event,
                        task.task_id,
                        EventType.TOOL_STARTED,
                        run_id=run.run_id,
                        payload={"idx": idx},
                    )

                await asyncio.gather(*[_emit_one(i) for i in range(6, 25)])
                # Emit terminal event 25
                coord.record_event(
                    task.task_id,
                    EventType.TASK_COMPLETED,
                    run_id=run.run_id,
                    payload={"idx": 25},
                )

            await asyncio.gather(consume_sse(), emit_concurrent_events())

        # Verify strict monotonic sequence 1..25 with ZERO gaps and ZERO duplicates
        assert received_seqs == list(range(1, 26))

        # Verify subscriber was cleaned up cleanly
        assert await get_event_broadcaster().get_subscriber_count(task.task_id) == 0
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_concurrent_single_flight_and_purge_active_guard(tmp_path: Path) -> None:
    """Verify atomic DB single-flight guard and purge rejection for QUEUED/CANCELLING tasks."""
    db_path = tmp_path / "single_flight_purge.db"
    coord = TaskCoordinator(db_path=db_path)
    deliv_mgr = DeliverableManager(db_path=db_path, workspace_root=tmp_path / "ws")
    task_svc = TaskService(coordinator=coord, deliverable_mgr=deliv_mgr)

    task = coord.create_task(query="Atomic guard test")

    # 1. Concurrent queue_run / start_run race: only ONE may succeed
    run1 = coord.queue_run(task.task_id, runtime_id="rt1")
    with pytest.raises(ValueError, match="cannot be queued while in state 'QUEUED'"):
        coord.queue_run(task.task_id, runtime_id="rt2")
    with pytest.raises(ValueError, match="cannot start a new run while in state 'QUEUED'"):
        coord.start_run(task.task_id, runtime_id="rt2", enforce_single_flight=True)

    # 2. Purge & Archive MUST reject QUEUED tasks (both TaskService and TaskCoordinator level)
    with pytest.raises(ValueError, match="cannot be purged while in active state 'QUEUED'"):
        task_svc.purge_task(task.task_id, confirm=True)
    with pytest.raises(ValueError, match="cannot be purged while in active state 'QUEUED'"):
        coord.purge_task(task.task_id)
    with pytest.raises(ValueError, match="cannot be archived while in active state 'QUEUED'"):
        task_svc.archive_task(task.task_id)

    # 3. Transition to CANCELLING -> purge MUST still reject
    coord.start_run(task.task_id, runtime_id="rt1", run_id=run1.run_id)
    coord.mark_cancelling(task.task_id, run1.run_id)
    with pytest.raises(ValueError, match="cannot be purged while in active state 'CANCELLING'"):
        task_svc.purge_task(task.task_id, confirm=True)
    with pytest.raises(ValueError, match="cannot be purged while in active state 'CANCELLING'"):
        coord.purge_task(task.task_id)


def test_checkpoint_gc_cleans_all_langgraph_tables(tmp_path: Path) -> None:
    """Verify clean_dangling_checkpoints removes rows from checkpoints, writes, checkpoint_blobs, and checkpoint_writes."""
    db_path = tmp_path / "gc_all_tables.db"
    coord = TaskCoordinator(db_path=db_path)
    active_task = coord.create_task(query="Keep me")

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
        conn.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_id TEXT)")
        conn.execute("CREATE TABLE checkpoint_blobs (thread_id TEXT, channel TEXT)")
        conn.execute("CREATE TABLE checkpoint_writes (thread_id TEXT, task_id TEXT)")

        for tid in (active_task.thread_id, "orphan_gc_1"):
            conn.execute("INSERT INTO checkpoints VALUES (?, ?)", (tid, "cp1"))
            conn.execute("INSERT INTO writes VALUES (?, ?)", (tid, "cp1"))
            conn.execute("INSERT INTO checkpoint_blobs VALUES (?, ?)", (tid, "messages"))
            conn.execute("INSERT INTO checkpoint_writes VALUES (?, ?)", (tid, "t1"))
        conn.commit()

    summary = clean_dangling_checkpoints(db_path)
    assert summary["purged_threads"] == 1
    assert summary["dangling_threads"] == ["orphan_gc_1"]

    with sqlite3.connect(str(db_path)) as conn:
        for tbl in ("checkpoints", "writes", "checkpoint_blobs", "checkpoint_writes"):
            rows = [r[0] for r in conn.execute(f"SELECT thread_id FROM {tbl}").fetchall()]
            assert rows == [active_task.thread_id]
