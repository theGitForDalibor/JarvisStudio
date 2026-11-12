"""Phase 2 Stage 4 tests: Per-thread async lock serialization, parallelism, and cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import ApprovalDecision, get_hitl_manager
from jarvis_studio.core.persistence.async_sqlite import open_managed_sqlite_checkpointer
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


class _FakeSlowGraph:
    """Async StateGraph double recording concurrency overlap per thread_id."""

    def __init__(self) -> None:
        self.checkpointer = None
        self.active_by_thread: dict[str, int] = {}
        self.max_overlap_by_thread: dict[str, int] = {}
        self.events: list[tuple[str, str]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}

    async def aget_state(self, config: dict[str, Any]):
        class _Snap:
            def __init__(self) -> None:
                self.values: dict[str, Any] = {}
                self.next: tuple[str, ...] = ()
                self.tasks: tuple[Any, ...] = ()

        return _Snap()

    async def ainvoke(self, input_state: Any, config: dict[str, Any]):
        tid = config["configurable"]["thread_id"]
        self.active_by_thread[tid] = self.active_by_thread.get(tid, 0) + 1
        self.max_overlap_by_thread[tid] = max(
            self.max_overlap_by_thread.get(tid, 0),
            self.active_by_thread[tid],
        )
        self.events.append(("enter", tid))
        if tid in self.entered:
            self.entered[tid].set()
        try:
            if tid in self.gates:
                await self.gates[tid].wait()
            else:
                await asyncio.sleep(0.03)
            msg = input_state["messages"][-1].content
            return {"messages": [AIMessage(content=f"reply:{msg}")]}
        finally:
            self.active_by_thread[tid] -= 1
            self.events.append(("exit", tid))

    async def astream_events(self, input_state: Any, config: dict[str, Any], version: str = "v2"):
        tid = config["configurable"]["thread_id"]
        self.active_by_thread[tid] = self.active_by_thread.get(tid, 0) + 1
        self.max_overlap_by_thread[tid] = max(
            self.max_overlap_by_thread.get(tid, 0),
            self.active_by_thread[tid],
        )
        try:
            yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="tok1 ")}}
            await asyncio.sleep(0.05)
            yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="tok2")}}
        finally:
            self.active_by_thread[tid] -= 1


@pytest.mark.asyncio
async def test_p2_th_01_same_thread_concurrent_calls_serialize_cleanly() -> None:
    """P2-TH-01: Concurrent calls on the same thread_id serialize without overlap."""
    fake_graph = _FakeSlowGraph()
    svc = AgentService(graph=fake_graph)

    r1, r2 = await asyncio.gather(
        svc.invoke_agent("sess-th-01", "first"),
        svc.invoke_agent("sess-th-01", "second"),
    )
    assert r1.status == "completed"
    assert r2.status == "completed"
    assert fake_graph.max_overlap_by_thread["sess-th-01"] == 1
    assert fake_graph.events == [
        ("enter", "sess-th-01"),
        ("exit", "sess-th-01"),
        ("enter", "sess-th-01"),
        ("exit", "sess-th-01"),
    ]


@pytest.mark.asyncio
async def test_p2_th_02_different_threads_do_not_block_each_other() -> None:
    """P2-TH-02: Blocked execution on thread_A does not delay independent thread_B."""
    fake_graph = _FakeSlowGraph()
    fake_graph.gates["sess-th-A"] = asyncio.Event()
    fake_graph.entered["sess-th-A"] = asyncio.Event()
    svc = AgentService(graph=fake_graph)

    task_a = asyncio.create_task(svc.invoke_agent("sess-th-A", "slow"))
    await fake_graph.entered["sess-th-A"].wait()

    # thread_B must complete immediately while thread_A stays blocked inside its lock
    res_b = await asyncio.wait_for(svc.invoke_agent("sess-th-B", "fast"), timeout=2.0)
    assert res_b.status == "completed"
    assert res_b.output_text == "reply:fast"

    fake_graph.gates["sess-th-A"].set()
    res_a = await task_a
    assert res_a.status == "completed"
    assert res_a.output_text == "reply:slow"


@pytest.mark.asyncio
async def test_p2_th_03_waiter_cancellation_preserves_lock_instance_and_cleans_entry() -> None:
    """P2-TH-03 (R2): Cancelling a waiting coroutine preserves the active lock entry & count."""
    svc = AgentService(graph=_FakeSlowGraph())
    loop = asyncio.get_running_loop()
    tid = "sess-race-03"

    t1_acquired = asyncio.Event()
    t1_release = asyncio.Event()
    order: list[str] = []

    async def _task1() -> None:
        async with svc._acquire_thread_lock(tid):
            order.append("t1-start")
            t1_acquired.set()
            await t1_release.wait()
            order.append("t1-end")

    async def _task_waiter(name: str) -> None:
        async with svc._acquire_thread_lock(tid):
            order.append(name)

    t1 = asyncio.create_task(_task1())
    await t1_acquired.wait()

    loop_locks = svc._thread_locks[loop]
    original_entry = loop_locks[tid]
    assert original_entry.waiters == 1

    # Queue Task 2 and Task 3 behind Task 1
    t2 = asyncio.create_task(_task_waiter("t2"))
    t3 = asyncio.create_task(_task_waiter("t3"))
    await asyncio.sleep(0)

    assert original_entry.waiters == 3
    assert loop_locks[tid] is original_entry

    # Cancel waiting Task 2: must raise CancelledError without releasing Task 1's lock
    # and without deleting the shared _ThreadLockEntry
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2

    assert original_entry.lock.locked() is True
    assert original_entry.waiters == 2
    assert loop_locks.get(tid) is original_entry

    # Release Task 1 -> Task 3 acquires the exact same Lock object and runs cleanly
    t1_release.set()
    await t1
    await t3

    assert order == ["t1-start", "t1-end", "t3"]
    assert tid not in svc._thread_locks.get(loop, {})


@pytest.mark.asyncio
async def test_stream_early_aclose_and_hold_cancel_release_thread_lock() -> None:
    """Early `.aclose()` on stream_agent and hold-time cancel cleanly unlock thread_id."""
    fake_graph = _FakeSlowGraph()
    svc = AgentService(graph=fake_graph)
    loop = asyncio.get_running_loop()
    tid = "sess-stream-cancel"

    agen = svc.stream_agent(tid, "first stream")
    seen_token = False
    try:
        async for ev in agen:
            if ev["event"] == "token":
                seen_token = True
                break
    finally:
        await agen.aclose()
    assert seen_token is True
    assert tid not in svc._thread_locks.get(loop, {})

    # Hold-time cancellation during invoke_agent must also release lock and clean entry
    fake_graph.gates[tid] = asyncio.Event()
    fake_graph.entered[tid] = asyncio.Event()
    task = asyncio.create_task(svc.invoke_agent(tid, "blocked"))
    await fake_graph.entered[tid].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tid not in svc._thread_locks.get(loop, {})
    fake_graph.gates.pop(tid)

    # Next request acquires fresh entry immediately without deadlock
    follow_up = await asyncio.wait_for(svc.invoke_agent(tid, "after cancel"), timeout=2.0)
    assert follow_up.status == "completed"


@pytest.mark.asyncio
async def test_resume_approval_serializes_without_reentrant_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resume_approval runs verify + Command(resume) under a single outer lock cleanly."""
    class _MockToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    async def _fake_exec(*args, **kwargs):
        return "ok-output"

    monkeypatch.setattr(
        "jarvis_studio.tools.builtin.shell_tools._execute_shell_command_async",
        _fake_exec,
    )

    reg = ToolRegistry()
    reg.register(execute_shell_command)
    hitl = get_hitl_manager()
    hitl.clear()

    managed = await open_managed_sqlite_checkpointer(tmp_path / "resume_lock.db")
    try:
        model = _MockToolModel(
            responses=[
                AIMessage(
                    content="Run echo",
                    tool_calls=[
                        {
                            "id": "call_r_1",
                            "name": "execute_shell_command",
                            "args": {"command": "echo hi"},
                        }
                    ],
                ),
                AIMessage(content="Done"),
            ]
        )
        graph = build_agent_graph(model=model, registry=reg, checkpointer=managed.saver)
        svc = AgentService(graph=graph, bound_loop=asyncio.get_running_loop())

        r1 = await svc.invoke_agent("sess-resume-lock", "run echo")
        assert r1.status == "interrupted"
        assert r1.pending_approval is not None
        appr_id = r1.pending_approval["approval_id"]

        resumed = await asyncio.wait_for(
            svc.resume_approval(
                "sess-resume-lock",
                appr_id,
                ApprovalDecision(decision="approve"),
            ),
            timeout=5.0,
        )
        assert resumed.status == "completed"
        assert resumed.output_text == "Done"
        assert "sess-resume-lock" not in svc._thread_locks.get(asyncio.get_running_loop(), {})
    finally:
        hitl.clear()
        await managed.aclose()
