"""Sprint 2A Runtime Integration Tests: Independent Tasks, AgentService, AsyncSqliteSaver, and HITL."""

from __future__ import annotations

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import ApprovalDecision, ApprovalStatus, ExecutionOutcomeStatus
from jarvis_studio.api.app import create_app
from jarvis_studio.api.routes import approvals as approvals_route_mod
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.persistence.sqlite import open_sync_connection
from jarvis_studio.core.runtime import AgentRuntime
from jarvis_studio.core.tasks import coordinator as task_coord_mod
from jarvis_studio.core.tasks.models import EventType, RunStatus, RunTriggerType, TaskStatus
from jarvis_studio.service import agent_service as agent_svc_mod
from jarvis_studio.service import deliverable_manager as deliv_mod
from jarvis_studio.service import session_service as session_svc_mod
from jarvis_studio.service import task_service as task_svc_mod
from jarvis_studio.service.session_service import SessionService
from jarvis_studio.service.task_service import TaskService
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety
from jarvis_studio.tools.registry import ToolRegistry


class _FakeToolChatModel(FakeMessagesListChatModel):
    """Fake chat model supporting LangGraph tool binding."""

    def bind_tools(self, tools, **kwargs):
        return self


class _FailingChatModel(FakeMessagesListChatModel):
    """Fake model whose async invocation fails deterministically."""

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, *args, **kwargs):
        raise RuntimeError("Simulated LLM outage during Run 1")


def _sqlite_settings(tmp_path) -> Settings:
    return Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(tmp_path / "jarvis_studio.db"),
            workspace_root=str(tmp_path / "workspace"),
        ),
        rag=RAGConfig(
            enabled=True,
            vector_db_path=str(tmp_path / "jarvis_vec.db"),
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_singletons():
    """Ensure clean singleton isolation before and after each runtime test."""
    for mod, attr in (
        (session_svc_mod, "_global_session_service"),
        (agent_svc_mod, "_global_agent_service"),
        (agent_svc_mod, "_default_agent_service"),
        (task_coord_mod, "_default_task_coordinator"),
        (deliv_mod, "_default_deliverable_manager"),
        (task_svc_mod, "_default_task_service"),
    ):
        obj = getattr(mod, attr, None)
        setattr(mod, attr, None)
        close_fn = getattr(obj, "close", None)
        if callable(close_fn):
            close_fn()
    try:
        yield
    finally:
        for mod, attr in (
            (session_svc_mod, "_global_session_service"),
            (agent_svc_mod, "_global_agent_service"),
            (agent_svc_mod, "_default_agent_service"),
            (task_coord_mod, "_default_task_coordinator"),
            (deliv_mod, "_default_deliverable_manager"),
            (task_svc_mod, "_default_task_service"),
        ):
            obj = getattr(mod, attr, None)
            setattr(mod, attr, None)
            close_fn = getattr(obj, "close", None)
            if callable(close_fn):
                close_fn()


@pytest.mark.asyncio
async def test_independent_task_drives_real_agent_runtime_without_ghost_sessions(tmp_path):
    """Independent task (`session_id=None`) executes end-to-end on AgentRuntime + AsyncSqliteSaver with 0 chat_sessions."""
    cfg = _sqlite_settings(tmp_path)
    safe_calls: list[str] = []

    @tool
    def lookup_spec(topic: str) -> str:
        """Look up technical spec safely."""
        safe_calls.append(topic)
        return f"Spec summary for {topic}"

    tag_tool_safety(lookup_spec, risk_level=RiskLevel.SAFE)
    reg = ToolRegistry()
    reg.register(lookup_spec)

    async with AgentRuntime.open(cfg, instance_id="rt-s2a-standalone", tool_registry=reg) as rt:
        model = _FakeToolChatModel(
            responses=[
                AIMessage(
                    content="Let me look up sqlite-vec",
                    tool_calls=[
                        {"id": "call_safe_1", "name": "lookup_spec", "args": {"topic": "sqlite-vec"}}
                    ],
                ),
                AIMessage(content="Standalone research finished successfully."),
            ]
        )
        rt.agent_service.graph = build_agent_graph(
            model=model,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )

        task_svc = TaskService(
            coordinator=rt.task_coordinator,
            db_path=cfg.storage.sqlite_path,
        )
        task, initial_run = task_svc.create_task(
            query="Analyze sqlite-vec independently",
            session_id=None,
            assigned_agent_id="research_agent",
        )
        assert initial_run is None
        assert task.session_id is None
        assert task.thread_id == task.task_id

        result = await task_svc.execute_task(task.task_id, agent_service=rt.agent_service)
        assert result.status == "completed"
        assert result.task_id == task.task_id
        assert result.thread_id == task.thread_id
        assert "Standalone research finished successfully." in result.output_text
        assert safe_calls == ["sqlite-vec"]

        # Verify persisted Task, Run, Events, Trace, and zero ghost sessions
        refreshed = task_svc.get_task(task.task_id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.COMPLETED
        assert refreshed.session_id is None
        assert refreshed.result_summary is not None
        assert "Standalone research finished" in refreshed.result_summary

        runs = rt.task_coordinator.list_runs(task.task_id)
        assert len(runs) == 1
        assert runs[0].run_seq == 1
        assert runs[0].status == RunStatus.COMPLETED
        assert runs[0].runtime_id == "rt-s2a-standalone"
        assert runs[0].checkpoint_id is not None

        events = task_svc.list_events(task.task_id)
        event_types = [e.event_type for e in events]
        assert EventType.TASK_CREATED in event_types
        assert EventType.RUN_STARTED in event_types
        assert EventType.TASK_COMPLETED in event_types

        with open_sync_connection(cfg.storage.sqlite_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0
            trace_row = conn.execute(
                "SELECT session_id, task_id, status FROM agent_conversation_traces WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()
            assert trace_row is not None
            assert trace_row[0] is None
            assert trace_row[1] == task.task_id
            assert trace_row[2] == "completed"


@pytest.mark.asyncio
async def test_explicit_task_id_reuses_task_without_creating_duplicate(tmp_path):
    """Passing explicit `task_id` to AgentService.invoke_agent runs against that task instead of creating a new one."""
    cfg = _sqlite_settings(tmp_path)
    reg = ToolRegistry()

    async with AgentRuntime.open(cfg, instance_id="rt-s2a-explicit", tool_registry=reg) as rt:
        model = _FakeToolChatModel(responses=[AIMessage(content="Executed explicit task.")])
        rt.agent_service.graph = build_agent_graph(
            model=model,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )

        task_svc = TaskService(coordinator=rt.task_coordinator, db_path=cfg.storage.sqlite_path)
        task, _ = task_svc.create_task(query="Original goal", session_id=None)

        result = await rt.agent_service.invoke_agent(
            task.thread_id,
            "Execute explicit task",
            task_id=task.task_id,
        )
        assert result.status == "completed"
        assert result.task_id == task.task_id

        all_tasks = rt.task_coordinator.list_tasks()
        assert len(all_tasks) == 1
        assert all_tasks[0].task_id == task.task_id
        assert all_tasks[0].status == TaskStatus.COMPLETED
        assert len(rt.task_coordinator.list_runs(task.task_id)) == 1


@pytest.mark.asyncio
async def test_multi_task_checkpoint_isolation_under_same_session(tmp_path):
    """Two Tasks linked to the same Session maintain completely isolated AsyncSqliteSaver checkpoints."""
    cfg = _sqlite_settings(tmp_path)
    reg = ToolRegistry()

    async with AgentRuntime.open(cfg, instance_id="rt-s2a-iso", tool_registry=reg) as rt:
        model = _FakeToolChatModel(
            responses=[
                AIMessage(content="Alpha output A"),
                AIMessage(content="Beta output B"),
            ]
        )
        rt.agent_service.graph = build_agent_graph(
            model=model,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )

        sess = rt._session_service.create_session("Shared Workspace Session")
        task_svc = TaskService(coordinator=rt.task_coordinator, db_path=cfg.storage.sqlite_path)
        task_a, _ = task_svc.create_task("Task Alpha goal", session_id=sess.session_id)
        task_b, _ = task_svc.create_task("Task Beta goal", session_id=sess.session_id)

        assert task_a.thread_id == task_a.task_id
        assert task_b.thread_id == task_b.task_id
        assert task_a.thread_id != task_b.thread_id

        res_a = await task_svc.execute_task(task_a.task_id, agent_service=rt.agent_service)
        res_b = await task_svc.execute_task(task_b.task_id, agent_service=rt.agent_service)
        assert res_a.output_text == "Alpha output A"
        assert res_b.output_text == "Beta output B"

        snap_a = await rt.agent_service.graph.aget_state({"configurable": {"thread_id": task_a.thread_id}})
        snap_b = await rt.agent_service.graph.aget_state({"configurable": {"thread_id": task_b.thread_id}})
        text_a = " ".join(str(m.content) for m in snap_a.values["messages"])
        text_b = " ".join(str(m.content) for m in snap_b.values["messages"])

        assert "Task Alpha goal" in text_a and "Alpha output A" in text_a
        assert "Task Beta goal" not in text_a and "Beta output B" not in text_a
        assert "Task Beta goal" in text_b and "Beta output B" in text_b
        assert "Task Alpha goal" not in text_b and "Alpha output A" not in text_b


@pytest.mark.asyncio
async def test_independent_task_hitl_interrupt_resume_and_anti_replay(tmp_path, monkeypatch):
    """Standalone Task supports HIGH-risk interrupt -> CAS resume via API & Service -> Consumable single execution -> Anti-replay."""
    cfg = _sqlite_settings(tmp_path)
    exec_count = 0

    @tool
    def danger_deploy(target: str) -> str:
        """High-risk deployment tool requiring HITL approval."""
        nonlocal exec_count
        exec_count += 1
        return f"Deployed to {target}"

    tag_tool_safety(danger_deploy, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg = ToolRegistry()
    reg.register(danger_deploy)

    async with AgentRuntime.open(cfg, instance_id="rt-s2a-hitl", tool_registry=reg) as rt:
        model = _FakeToolChatModel(
            responses=[
                AIMessage(
                    content="Need deploy approval",
                    tool_calls=[
                        {"id": "call_dep_1", "name": "danger_deploy", "args": {"target": "prod"}}
                    ],
                ),
                AIMessage(content="Deployment succeeded after approval."),
            ]
        )
        rt.agent_service.graph = build_agent_graph(
            model=model,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )

        task_svc = TaskService(coordinator=rt.task_coordinator, db_path=cfg.storage.sqlite_path)
        task, _ = task_svc.create_task("Deploy service safely", session_id=None)

        # 1. Initial Run #1 hits approval_gate
        res1 = await task_svc.execute_task(task.task_id, agent_service=rt.agent_service)
        assert res1.status == "interrupted"
        assert res1.task_id == task.task_id
        assert res1.pending_approval is not None
        approval_id = res1.pending_approval["approval_id"]
        assert exec_count == 0

        t_waiting = task_svc.get_task(task.task_id)
        assert t_waiting.status == TaskStatus.WAITING_APPROVAL
        runs1 = rt.task_coordinator.list_runs(task.task_id)
        assert len(runs1) == 1
        assert runs1[0].run_seq == 1
        assert runs1[0].status == RunStatus.INTERRUPTED
        assert runs1[0].checkpoint_id is not None

        # Normal re-execute while WAITING_APPROVAL is blocked
        with pytest.raises(ValueError, match="WAITING_APPROVAL"):
            await task_svc.execute_task(task.task_id, agent_service=rt.agent_service)

        # 2. Verify /api/v1/approvals recognizes standalone task thread_id via HTTP
        monkeypatch.setattr(approvals_route_mod, "get_agent_service", lambda: rt.agent_service)
        monkeypatch.setattr(approvals_route_mod, "get_session_service", lambda: rt._session_service)
        monkeypatch.setattr(approvals_route_mod, "get_hitl_manager", lambda: rt.hitl_manager)
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            list_resp = await http_client.get(f"/api/v1/approvals?thread_id={task.thread_id}")
            assert list_resp.status_code == 200
            pending_items = list_resp.json()
            assert len(pending_items) == 1
            assert pending_items[0]["id"] == approval_id
            assert pending_items[0]["task_id"] == task.task_id

            # Submit approval decision over REST API -> triggers Run #2!
            dec_resp = await http_client.post(
                f"/api/v1/approvals/{approval_id}/decision",
                json={"thread_id": task.thread_id, "decision": "approve", "reason": "Verified safe"},
            )
            assert dec_resp.status_code == 200
            dec_data = dec_resp.json()
            assert dec_data["status"] == "completed"
            assert "Deployment succeeded" in dec_data["output_text"]
            assert exec_count == 1

            # 3. Anti-replay: second decision submission is rejected (404) & tool does not re-run
            replay_resp = await http_client.post(
                f"/api/v1/approvals/{approval_id}/decision",
                json={"thread_id": task.thread_id, "decision": "approve"},
            )
            assert replay_resp.status_code == 404
            assert exec_count == 1

        # Also direct service-level replay blocked
        replay_svc_res = await task_svc.resume_task_approval(
            task.task_id,
            approval_id,
            ApprovalDecision(decision="approve"),
            agent_service=rt.agent_service,
        )
        assert replay_svc_res.status == "error"
        assert exec_count == 1

        # Verify Run #2, Approval CONSUMED, Outcome SUCCEEDED, and ZERO ghost chat_sessions
        req = rt.hitl_manager.get_request(approval_id, thread_id=task.thread_id)
        assert req is not None and req.status == ApprovalStatus.CONSUMED
        outcome = rt.hitl_manager.get_outcome_by_approval(approval_id, thread_id=task.thread_id)
        assert outcome is not None and outcome.outcome_status == ExecutionOutcomeStatus.SUCCEEDED

        runs_after = rt.task_coordinator.list_runs(task.task_id)
        assert [r.run_seq for r in runs_after] == [1, 2]
        assert runs_after[1].trigger_type == RunTriggerType.HITL_RESUME
        assert runs_after[1].status == RunStatus.COMPLETED

        with open_sync_connection(cfg.storage.sqlite_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_startup_recovery_and_unknown_invariant_for_independent_tasks(tmp_path):
    """Startup recovery preserves pending standalone approval and enforces UNKNOWN no-auto-retry after crash."""
    cfg = _sqlite_settings(tmp_path)
    exec_count = 0

    @tool
    def danger_op(x: str) -> str:
        """High-risk op."""
        nonlocal exec_count
        exec_count += 1
        return f"op:{x}"

    tag_tool_safety(danger_op, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg = ToolRegistry()
    reg.register(danger_op)

    # Phase A: Task 1 leaves valid PENDING interrupt + SQLite checkpoint; Task 2 simulates EXECUTING crash
    async with AgentRuntime.open(cfg, instance_id="rt-s2a-crash-a", tool_registry=reg) as rt_a:
        model_a = _FakeToolChatModel(
            responses=[
                AIMessage(
                    content="Need approval",
                    tool_calls=[{"id": "call_rec_1", "name": "danger_op", "args": {"x": "keep"}}],
                )
            ]
        )
        rt_a.agent_service.graph = build_agent_graph(
            model=model_a,
            registry=reg,
            checkpointer=rt_a.checkpointer,
            hitl_mgr=rt_a.hitl_manager,
        )
        t_pending = rt_a.task_coordinator.create_task(session_id=None, query="Pending independent task")
        res1 = await rt_a.agent_service.execute_task(t_pending.task_id)
        assert res1.status == "interrupted"
        appr_pending_id = res1.pending_approval["approval_id"]

        # Task 2: Simulate active EXECUTING lease when process dies
        t_crashed = rt_a.task_coordinator.create_task(session_id=None, query="Crashed executing task")
        r_crashed = rt_a.task_coordinator.start_run(t_crashed.task_id, runtime_id="rt-s2a-crash-a")
        rt_a.task_coordinator.set_task_status(t_crashed.task_id, TaskStatus.EXECUTING_TOOL)
        appr_crashed = rt_a.hitl_manager.create_request(
            thread_id=t_crashed.thread_id,
            run_id=r_crashed.run_id,
            task_id=t_crashed.task_id,
            tool_name="danger_op",
            arguments={"x": "crashed"},
            tool_call_id="call_crash_2",
            description="crashed op",
        )
        rt_a.hitl_manager.resolve_request(
            appr_crashed.id, ApprovalDecision(decision="approve"), thread_id=t_crashed.thread_id
        )
        ok_lease, _, _ = rt_a.hitl_manager.acquire_execution_lease(
            appr_crashed.id,
            thread_id=t_crashed.thread_id,
            tool_call_id=appr_crashed.tool_call_id,
            arguments_hash=appr_crashed.arguments_hash,
            lease_owner_instance_id="rt-s2a-crash-a",
            lease_acquired_run_id=r_crashed.run_id,
        )
        assert ok_lease is True

    # Phase B: Restart AgentRuntime on the same SQLite DB
    async with AgentRuntime.open(cfg, instance_id="rt-s2a-recover-b", tool_registry=reg) as rt_b:
        assert rt_b.startup_recovery is not None
        assert rt_b.startup_recovery.kept_pending == 1
        assert rt_b.startup_recovery.stale_executing_marked_unknown == 1

        # Crashed Task 2 must be reconciled to UNKNOWN and refuse auto re-execution!
        t2_rec = rt_b.task_coordinator.get_task(t_crashed.task_id)
        assert t2_rec is not None and t2_rec.status == TaskStatus.UNKNOWN
        assert rt_b.hitl_manager.get_request(appr_crashed.id).status == ApprovalStatus.UNKNOWN
        with pytest.raises(RuntimeError, match="UNKNOWN"):
            await rt_b.agent_service.execute_task(t_crashed.task_id)

        # Pending Task 1 can safely resume on Run #2 in Runtime B!
        model_b = _FakeToolChatModel(responses=[AIMessage(content="Recovered standalone task!")])
        rt_b.agent_service.graph = build_agent_graph(
            model=model_b,
            registry=reg,
            checkpointer=rt_b.checkpointer,
            hitl_mgr=rt_b.hitl_manager,
        )
        res_rec = await rt_b.agent_service.resume_task_approval(
            t_pending.task_id,
            appr_pending_id,
            ApprovalDecision(decision="approve"),
        )
        assert res_rec.status == "completed"
        assert "Recovered standalone task!" in res_rec.output_text
        assert exec_count == 1
        assert rt_b.task_coordinator.get_task(t_pending.task_id).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_multi_run_fresh_checkpoint_retry_after_failed_run(tmp_path):
    """Retrying a FAILED task with `fresh_checkpoint=True` clears dirty Run 1 state and completes Run 2 cleanly."""
    cfg = _sqlite_settings(tmp_path)
    reg = ToolRegistry()

    async with AgentRuntime.open(cfg, instance_id="rt-s2a-retry", tool_registry=reg) as rt:
        # Run 1 fails
        rt.agent_service.graph = build_agent_graph(
            model=_FailingChatModel(responses=[]),
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )
        task_svc = TaskService(coordinator=rt.task_coordinator, db_path=cfg.storage.sqlite_path)
        task, _ = task_svc.create_task("Retryable query", session_id=None)

        res1 = await task_svc.execute_task(task.task_id, agent_service=rt.agent_service)
        assert res1.status == "error"
        assert task_svc.get_task(task.task_id).status == TaskStatus.FAILED
        assert rt.task_coordinator.list_runs(task.task_id)[0].status == RunStatus.FAILED

        # Run 2 with fresh_checkpoint=True clears prior dirty thread checkpoint and completes
        rt.agent_service.graph = build_agent_graph(
            model=_FakeToolChatModel(responses=[AIMessage(content="Clean Run 2 success")]),
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )
        res2 = await task_svc.execute_task(
            task.task_id,
            agent_service=rt.agent_service,
            user_message="Fresh retry input",
            fresh_checkpoint=True,
        )
        assert res2.status == "completed"
        assert res2.output_text == "Clean Run 2 success"

        runs = rt.task_coordinator.list_runs(task.task_id)
        assert [r.run_seq for r in runs] == [1, 2]
        assert [r.status for r in runs] == [RunStatus.FAILED, RunStatus.COMPLETED]

        snap = await rt.agent_service.graph.aget_state({"configurable": {"thread_id": task.thread_id}})
        msg_texts = [str(m.content) for m in snap.values["messages"]]
        # Old failing Run 1 query must have been cleared by adelete_thread(fresh_checkpoint=True)
        assert "Retryable query" not in msg_texts
        assert "Fresh retry input" in msg_texts
        assert "Clean Run 2 success" in msg_texts
