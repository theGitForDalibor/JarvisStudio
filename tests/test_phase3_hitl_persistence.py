"""Phase 3 tests: SQLite HITL persistence, Tool Execution Outcomes, Option B & Startup Recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    ExecutionOutcomeStatus,
    HITLManager,
)
from jarvis_studio.agent.nodes.executor import create_tool_executor_node
from jarvis_studio.api.app import create_app
from jarvis_studio.config import settings as settings_mod
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.persistence.migrations import (
    APP_SCHEMA_VERSION,
    run_app_migrations,
)
from jarvis_studio.core.persistence.recovery import reconcile_on_startup
from jarvis_studio.core.persistence.runtime_lock import RuntimeInstanceLock
from jarvis_studio.core.persistence.sqlite import open_sync_connection
from jarvis_studio.core.runtime import AgentRuntime
from jarvis_studio.service import agent_service as agent_svc_mod
from jarvis_studio.service import session_service as session_svc_mod
from jarvis_studio.service.session_service import SessionService


@pytest.fixture(autouse=True)
def _isolate_phase3_singletons():
    """Ensure Phase 3 tests start and end without stale cached singletons or open DB connections."""
    for mod, attr in (
        (session_svc_mod, "_global_session_service"),
        (agent_svc_mod, "_default_agent_service"),
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
            (agent_svc_mod, "_default_agent_service"),
        ):
            obj = getattr(mod, attr, None)
            setattr(mod, attr, None)
            close_fn = getattr(obj, "close", None)
            if callable(close_fn):
                close_fn()
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety
from jarvis_studio.tools.registry import ToolRegistry


class _MockToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _make_sqlite_settings(tmp_path: Path) -> Settings:
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


def test_p3_mig_01_v1_to_v2_migration_and_sqlite_constraints(tmp_path: Path) -> None:
    """v1 -> v2 migration preserves v1 data and enforces CHECK/UNIQUE constraints."""
    db_path = tmp_path / "upgrade_v1_to_v2.db"

    # Seed a genuine v1 schema database with pre-existing session/trace data
    conn = open_sync_connection(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT
            );
            INSERT INTO chat_sessions VALUES ('s-v1', 'Legacy Session', 1.0, 1.0, NULL);
            PRAGMA user_version = 1;
            """
        )
    finally:
        conn.close()

    assert run_app_migrations(db_path) == APP_SCHEMA_VERSION
    assert APP_SCHEMA_VERSION == 2
    assert len(list(tmp_path.glob("upgrade_v1_to_v2.bak.v1_to_v2.*.db"))) == 1

    check_conn = open_sync_connection(db_path)
    try:
        assert check_conn.execute("PRAGMA user_version").fetchone()[0] == 2
        sess = check_conn.execute("SELECT title FROM chat_sessions WHERE session_id='s-v1'").fetchone()
        assert sess is not None and sess[0] == "Legacy Session"

        # CHECK constraint rejects 'default' and empty thread_id / tool_call_id
        with pytest.raises(sqlite3.IntegrityError):
            check_conn.execute(
                """
                INSERT INTO hitl_approval_requests (
                    id, thread_id, run_id, tool_call_id, tool_name,
                    arguments_json, arguments_hash, risk_level, status, description, created_at
                ) VALUES ('appr_bad1', 'default', 'r1', 'c1', 't1', '{}', 'h1', 'HIGH', 'PENDING', 'd', 1.0)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            check_conn.execute(
                """
                INSERT INTO tool_execution_outcomes (
                    outcome_id, thread_id, run_id, tool_call_id, tool_name,
                    is_high_risk, outcome_status, started_at, updated_at
                ) VALUES ('out_bad1', '   ', 'r1', 'c1', 't1', 0, 'NOT_STARTED', 1.0, 1.0)
                """
            )
    finally:
        check_conn.close()

    # Python layer also rejects empty / 'default' thread_id and empty tool_call_id
    mgr = HITLManager(db_path=db_path)
    try:
        for bad_tid in ("", "   ", "default", "DEFAULT"):
            with pytest.raises(ValueError, match="Invalid thread_id"):
                mgr.create_request(
                    thread_id=bad_tid,
                    run_id="r1",
                    tool_name="execute_shell_command",
                    arguments={"command": "echo x"},
                    tool_call_id="c1",
                )
        with pytest.raises(ValueError, match="tool_call_id"):
            mgr.create_request(
                thread_id="valid-tid",
                run_id="r1",
                tool_name="execute_shell_command",
                arguments={"command": "echo x"},
                tool_call_id="",
            )
    finally:
        mgr.close()


@pytest.mark.asyncio
async def test_p3_fi_01_pending_interrupt_persists_and_resumes_across_runtime_restart(
    tmp_path: Path,
) -> None:
    """FI-01: PENDING approval + checkpoint survive full Runtime restart and execute strictly once."""
    cfg = _make_sqlite_settings(tmp_path)
    exec_count = 0

    @tool
    def danger_write(target: str) -> str:
        """Perform a tracked dangerous write."""
        nonlocal exec_count
        exec_count += 1
        return f"WROTE:{target}"

    tag_tool_safety(danger_write, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg = ToolRegistry()
    reg.register(danger_write)

    thread_id = "sess-fi01"
    session_svc = SessionService(db_path=cfg.storage.sqlite_path)
    session_svc.create_session(session_id=thread_id, title="FI-01")
    session_svc.close()

    # 1. Runtime A: hits interrupt and persists PENDING approval + checkpoint
    async with AgentRuntime.open(cfg, instance_id="rt-fi01-a", tool_registry=reg) as rt_a:
        model_a = _MockToolModel(
            responses=[
                AIMessage(
                    content="Need danger_write",
                    tool_calls=[
                        {"id": "call_fi01", "name": "danger_write", "args": {"target": "a.txt"}}
                    ],
                ),
                AIMessage(content="Completed on A"),
            ]
        )
        rt_a.agent_service.graph = build_agent_graph(
            model=model_a,
            registry=reg,
            checkpointer=rt_a.checkpointer,
            hitl_mgr=rt_a.hitl_manager,
        )
        res_a = await rt_a.agent_service.invoke_agent(thread_id, "write a.txt")
        assert res_a.status == "interrupted"
        assert res_a.pending_approval is not None
        approval_id = res_a.pending_approval["approval_id"]
        assert exec_count == 0

    # 2. Runtime B: brand-new process/runtime recovers PENDING and resumes cleanly
    async with AgentRuntime.open(cfg, instance_id="rt-fi01-b", tool_registry=reg) as rt_b:
        assert rt_b.startup_recovery is not None
        assert rt_b.startup_recovery.kept_pending == 1
        assert rt_b.startup_recovery.orphan_pending_marked_failed == 0

        model_b = _MockToolModel(responses=[AIMessage(content="Completed after restart")])
        rt_b.agent_service.graph = build_agent_graph(
            model=model_b,
            registry=reg,
            checkpointer=rt_b.checkpointer,
            hitl_mgr=rt_b.hitl_manager,
        )
        assert await rt_b.agent_service.verify_approval_context(thread_id, approval_id) is True

        res_b = await rt_b.agent_service.resume_approval(
            thread_id, approval_id, ApprovalDecision(decision="approve")
        )
        assert res_b.status == "completed"
        assert "Completed after restart" in res_b.output_text
        assert exec_count == 1

        req = rt_b.hitl_manager.get_request(approval_id, thread_id=thread_id)
        assert req is not None and req.status == ApprovalStatus.CONSUMED
        outcome = rt_b.hitl_manager.get_outcome_by_approval(approval_id, thread_id=thread_id)
        assert outcome is not None and outcome.outcome_status == ExecutionOutcomeStatus.SUCCEEDED

        # Replay after restart/completion is strictly blocked
        assert await rt_b.agent_service.verify_approval_context(thread_id, approval_id) is False


@pytest.mark.asyncio
async def test_p3_fi_02_approved_before_resume_does_not_auto_run_and_blocks_overwrite(
    tmp_path: Path,
) -> None:
    """FI-02: APPROVED before Command(resume) stays paused on restart, blocks decision overwrite, and supports abort/resume."""
    cfg = _make_sqlite_settings(tmp_path)
    exec_count = 0

    @tool
    def danger_action(val: str) -> str:
        """Tracked dangerous action."""
        nonlocal exec_count
        exec_count += 1
        return f"ACT:{val}"

    tag_tool_safety(danger_action, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg = ToolRegistry()
    reg.register(danger_action)

    session_svc = SessionService(db_path=cfg.storage.sqlite_path)
    session_svc.create_session(session_id="sess-fi02-run")
    session_svc.create_session(session_id="sess-fi02-abort")
    session_svc.close()

    # Runtime A: pause & resolve -> APPROVED (never entered EXECUTING)
    async with AgentRuntime.open(cfg, instance_id="rt-fi02-a", tool_registry=reg) as rt_a:
        for tid, call_id in (("sess-fi02-run", "c-run"), ("sess-fi02-abort", "c-abort")):
            model_a = _MockToolModel(
                responses=[
                    AIMessage(
                        content="Need action",
                        tool_calls=[{"id": call_id, "name": "danger_action", "args": {"val": tid}}],
                    ),
                    AIMessage(content="done"),
                ]
            )
            rt_a.agent_service.graph = build_agent_graph(
                model=model_a,
                registry=reg,
                checkpointer=rt_a.checkpointer,
                hitl_mgr=rt_a.hitl_manager,
            )
            res = await rt_a.agent_service.invoke_agent(tid, "go")
            assert res.status == "interrupted"
            appr_id = res.pending_approval["approval_id"]  # type: ignore[index]
            rt_a.hitl_manager.resolve_request(
                appr_id, ApprovalDecision(decision="approve"), thread_id=tid
            )

    assert exec_count == 0

    # Runtime B: restart must NOT auto-execute; keeps APPROVED for explicit resume or abort
    async with AgentRuntime.open(cfg, instance_id="rt-fi02-b", tool_registry=reg) as rt_b:
        assert exec_count == 0
        assert rt_b.startup_recovery is not None
        assert rt_b.startup_recovery.kept_approved_for_explicit_resume == 2

        model_b = _MockToolModel(responses=[AIMessage(content="resumed-run"), AIMessage(content="aborted-run")])
        rt_b.agent_service.graph = build_agent_graph(
            model=model_b,
            registry=reg,
            checkpointer=rt_b.checkpointer,
            hitl_mgr=rt_b.hitl_manager,
        )

        req_run = rt_b.hitl_manager.get_by_tool_call_id("c-run", "sess-fi02-run")
        req_abort = rt_b.hitl_manager.get_by_tool_call_id("c-abort", "sess-fi02-abort")
        assert req_run is not None and req_abort is not None

        # Cannot overwrite persisted approve decision with modify
        overwrite_res = await rt_b.agent_service.resume_approval(
            "sess-fi02-run",
            req_run.id,
            ApprovalDecision(decision="modify", modified_arguments={"val": "evil"}),
        )
        assert overwrite_res.status == "error"
        assert "Cannot overwrite persisted approval decision" in (overwrite_res.error or "")
        assert exec_count == 0

        # Explicit abort transitions to FAILED without ever running tool
        abort_res = await rt_b.agent_service.resume_approval(
            "sess-fi02-abort", req_abort.id, ApprovalDecision(decision="reject", reason="user cancelled")
        )
        assert abort_res.status == "completed"
        assert exec_count == 0
        aborted_req = rt_b.hitl_manager.get_request(req_abort.id, thread_id="sess-fi02-abort")
        assert aborted_req is not None and aborted_req.status == ApprovalStatus.FAILED

        # Explicit resume confirmation runs tool strictly once
        run_res = await rt_b.agent_service.resume_approval(
            "sess-fi02-run", req_run.id, ApprovalDecision(decision="approve")
        )
        assert run_res.status == "completed"
        assert exec_count == 1
        final_req = rt_b.hitl_manager.get_request(req_run.id, thread_id="sess-fi02-run")
        assert final_req is not None and final_req.status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_p3_fi_03_04_stale_executing_to_unknown_and_human_audit_never_reactivates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FI-03/FI-04: Dead EXECUTING (even with NOT_STARTED outcome) -> UNKNOWN; human audit never reactivates lease."""
    cfg = _make_sqlite_settings(tmp_path)
    monkeypatch.setattr(settings_mod, "_global_settings", cfg)
    session_svc = SessionService(db_path=cfg.storage.sqlite_path)
    session_svc.create_session(session_id="sess-fi03", title="FI-03")
    session_svc.close()

    # 1. Runtime A acquires lease (Approval=EXECUTING, Outcome=NOT_STARTED) and exits without finalizing
    async with AgentRuntime.open(cfg, instance_id="rt-crashed") as rt_a:
        req = rt_a.hitl_manager.create_request(
            thread_id="sess-fi03",
            run_id="run-crash",
            tool_name="execute_shell_command",
            arguments={"command": "echo crash"},
            tool_call_id="call_crash_03",
        )
        rt_a.hitl_manager.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id="sess-fi03")
        ok, _, err = rt_a.hitl_manager.acquire_execution_lease(
            approval_id=req.id,
            thread_id="sess-fi03",
            tool_call_id="call_crash_03",
            arguments_hash=req.arguments_hash,
        )
        assert ok is True and err is None
        out_before = rt_a.hitl_manager.get_outcome_by_approval(req.id, thread_id="sess-fi03")
        assert out_before is not None and out_before.outcome_status == ExecutionOutcomeStatus.NOT_STARTED
        appr_id = req.id
        args_hash = req.arguments_hash

    # 2. Runtime B starts under lock: NOT_STARTED must still converge to Approval=UNKNOWN + Outcome=UNKNOWN
    async with AgentRuntime.open(cfg, instance_id="rt-recovered") as rt_b:
        assert rt_b.startup_recovery is not None
        assert rt_b.startup_recovery.stale_executing_marked_unknown == 1

        rec_req = rt_b.hitl_manager.get_request(appr_id, thread_id="sess-fi03")
        rec_out = rt_b.hitl_manager.get_outcome_by_approval(appr_id, thread_id="sess-fi03")
        assert rec_req is not None and rec_req.status == ApprovalStatus.UNKNOWN
        assert rec_out is not None and rec_out.outcome_status == ExecutionOutcomeStatus.UNKNOWN

        # Replaying original approval_id is blocked
        replay_ok, _, replay_err = rt_b.hitl_manager.acquire_execution_lease(
            approval_id=appr_id,
            thread_id="sess-fi03",
            tool_call_id="call_crash_03",
            arguments_hash=args_hash,
        )
        assert replay_ok is False
        assert "UNKNOWN" in (replay_err or "")

    # 3. Human audit via API updates Outcome to FAILED/SUCCEEDED without reactivating approval_id
    app = create_app()
    with TestClient(app) as client:
        r_out = client.get(f"/api/v1/approvals/{appr_id}/outcome?thread_id=sess-fi03")
        assert r_out.status_code == 200
        assert r_out.json()["outcome_status"] == "UNKNOWN"

        # Cross-thread audit/outcome access returns uniform 404
        r_cross = client.get(f"/api/v1/approvals/{appr_id}/outcome?thread_id=other-sess")
        assert r_cross.status_code == 404

        r_audit = client.post(
            f"/api/v1/approvals/{appr_id}/audit",
            json={
                "thread_id": "sess-fi03",
                "verified_status": "FAILED",
                "actor": "local_admin",
                "note": "Verified no file created",
            },
        )
        assert r_audit.status_code == 200
        assert r_audit.json()["outcome_status"] == "FAILED"
        assert r_audit.json()["reconciled_by"] == "local_admin"

        rt_live = app.state.agent_runtime
        audited_req = rt_live.hitl_manager.get_request(appr_id, thread_id="sess-fi03")
        assert audited_req is not None
        assert audited_req.status == ApprovalStatus.UNKNOWN
        assert audited_req.audit_actor == "local_admin"
        assert audited_req.audit_note == "Verified no file created"

        # Even after human audit marked outcome FAILED, original approval_id stays locked
        still_blocked, _, _ = rt_live.hitl_manager.acquire_execution_lease(
            approval_id=appr_id,
            thread_id="sess-fi03",
            tool_call_id="call_crash_03",
            arguments_hash=args_hash,
        )
        assert still_blocked is False


@pytest.mark.asyncio
async def test_p3_fi_05_safe_tool_outcomes_fail_closed_and_replay_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FI-05: Tracked SAFE tools persist NOT_STARTED -> SUCCEEDED, block replay, fail closed on pre-write error, and recover stale NOT_STARTED -> UNKNOWN."""
    cfg = _make_sqlite_settings(tmp_path)
    safe_calls = 0

    @tool
    def tracked_safe_tool(key: str) -> str:
        """Safe tool with side effect."""
        nonlocal safe_calls
        safe_calls += 1
        return f"SAVED:{key}"

    tag_tool_safety(tracked_safe_tool, risk_level=RiskLevel.SAFE, requires_approval=False)
    reg = ToolRegistry()
    reg.register(tracked_safe_tool)

    hitl = HITLManager(db_path=cfg.storage.sqlite_path)
    try:
        executor = create_tool_executor_node(reg, hitl_mgr=hitl)
        state = {
            "messages": [
                AIMessage(
                    content="save",
                    tool_calls=[
                        {"id": "call_safe_1", "name": "tracked_safe_tool", "args": {"key": "k1"}}
                    ],
                )
            ],
            "run_context": {"session_id": "sess-safe", "run_id": "run-safe-1"},
        }

        # 1. Normal execution creates NOT_STARTED -> SUCCEEDED
        res1 = await executor(state)
        assert safe_calls == 1
        assert "SAVED:k1" in res1["messages"][0].content
        out1 = hitl.get_outcome("sess-safe", "call_safe_1")
        assert out1 is not None
        assert out1.outcome_status == ExecutionOutcomeStatus.SUCCEEDED
        assert out1.is_high_risk is False
        assert out1.approval_id is None

        # 2. Duplicate replay of same (thread_id, tool_call_id) is blocked by UNIQUE constraint
        res2 = await executor(state)
        assert safe_calls == 1
        assert "Operation cancelled" in res2["messages"][0].content
        assert "replay blocked" in res2["messages"][0].content

        # 3. Pre-execution persistence failure -> Fail Closed (tool NOT invoked)
        def _boom_start(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(hitl, "register_safe_outcome_start", _boom_start)
        fail_state = {
            "messages": [
                AIMessage(
                    content="save2",
                    tool_calls=[
                        {"id": "call_safe_2", "name": "tracked_safe_tool", "args": {"key": "k2"}}
                    ],
                )
            ],
            "run_context": {"session_id": "sess-safe", "run_id": "run-safe-2"},
        }
        res3 = await executor(fail_state)
        assert safe_calls == 1
        assert "Operation cancelled" in res3["messages"][0].content
        monkeypatch.undo()

        # 4. Post-execution outcome update failure preserves output + warns + leaves NOT_STARTED
        def _boom_fin(*args, **kwargs):
            raise RuntimeError("outcome update failed")

        monkeypatch.setattr(hitl, "finalize_safe_outcome", _boom_fin)
        fin_state = {
            "messages": [
                AIMessage(
                    content="save3",
                    tool_calls=[
                        {"id": "call_safe_3", "name": "tracked_safe_tool", "args": {"key": "k3"}}
                    ],
                )
            ],
            "run_context": {"session_id": "sess-safe", "run_id": "run-safe-3"},
        }
        res4 = await executor(fin_state)
        assert safe_calls == 2
        msg4 = res4["messages"][0]
        assert isinstance(msg4, ToolMessage)
        assert "SAVED:k3" in msg4.content
        assert "OUTCOME_FINALIZATION_ERROR" in msg4.content
        assert msg4.additional_kwargs.get("tool_succeeded") is True
        assert hitl.get_outcome("sess-safe", "call_safe_3").outcome_status == ExecutionOutcomeStatus.NOT_STARTED  # type: ignore[union-attr]
        monkeypatch.undo()
    finally:
        hitl.close()

    # 5. Restarting AgentRuntime converges stale SAFE NOT_STARTED -> UNKNOWN
    async with AgentRuntime.open(cfg, instance_id="rt-safe-recover") as rt:
        assert rt.startup_recovery is not None
        assert rt.startup_recovery.stale_safe_outcomes_marked_unknown == 1
        recovered_out = rt.hitl_manager.get_outcome("sess-safe", "call_safe_3")
        assert recovered_out is not None
        assert recovered_out.outcome_status == ExecutionOutcomeStatus.UNKNOWN


@pytest.mark.asyncio
async def test_p3_fi_06_option_b_in_flight_cancelled_error_marks_outcome_unknown(
    tmp_path: Path,
) -> None:
    """FI-06 (Option B): In-flight CancelledError keeps Approval=FAILED (1A/1B compat) and sets Outcome=UNKNOWN."""
    cfg = _make_sqlite_settings(tmp_path)
    started_high = asyncio.Event()
    started_safe = asyncio.Event()
    never = asyncio.Event()

    @tool
    async def blocking_high_tool(x: str) -> str:
        """High-risk blocking tool."""
        started_high.set()
        await never.wait()
        return x

    @tool
    async def blocking_safe_tool(x: str) -> str:
        """Safe blocking tool."""
        started_safe.set()
        await never.wait()
        return x

    tag_tool_safety(blocking_high_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    tag_tool_safety(blocking_safe_tool, risk_level=RiskLevel.SAFE, requires_approval=False)
    reg = ToolRegistry()
    reg.register(blocking_high_tool)
    reg.register(blocking_safe_tool)

    hitl = HITLManager(db_path=cfg.storage.sqlite_path, runtime_instance_id="rt-optb")
    try:
        executor = create_tool_executor_node(reg, hitl_mgr=hitl)

        # High-risk in-flight cancel
        req = hitl.create_request(
            thread_id="sess-optb",
            run_id="run-optb",
            tool_name="blocking_high_tool",
            arguments={"x": "1"},
            tool_call_id="call_optb_high",
        )
        hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id="sess-optb")
        high_state = {
            "messages": [
                AIMessage(
                    content="run",
                    tool_calls=[{"id": "call_optb_high", "name": "blocking_high_tool", "args": {"x": "1"}}],
                )
            ],
            "run_context": {"session_id": "sess-optb", "run_id": "run-optb"},
            "approval_decision": {
                "decisions_by_call_id": {
                    "call_optb_high": {
                        "approval_id": req.id,
                        "tool_call_id": "call_optb_high",
                        "decision": "approve",
                        "arguments_hash": req.arguments_hash,
                    }
                }
            },
        }
        t_high = asyncio.create_task(executor(high_state))
        await started_high.wait()
        t_high.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t_high

        updated_req = hitl.get_request(req.id, thread_id="sess-optb")
        updated_out = hitl.get_outcome_by_approval(req.id, thread_id="sess-optb")
        assert updated_req is not None and updated_req.status == ApprovalStatus.FAILED
        assert updated_out is not None and updated_out.outcome_status == ExecutionOutcomeStatus.UNKNOWN

        # Safe tool in-flight cancel
        safe_state = {
            "messages": [
                AIMessage(
                    content="run",
                    tool_calls=[{"id": "call_optb_safe", "name": "blocking_safe_tool", "args": {"x": "2"}}],
                )
            ],
            "run_context": {"session_id": "sess-optb", "run_id": "run-optb"},
        }
        t_safe = asyncio.create_task(executor(safe_state))
        await started_safe.wait()
        t_safe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t_safe

        safe_out = hitl.get_outcome("sess-optb", "call_optb_safe")
        assert safe_out is not None and safe_out.outcome_status == ExecutionOutcomeStatus.UNKNOWN
    finally:
        hitl.close()


@pytest.mark.asyncio
async def test_p3_fi_07_orphan_pending_and_lock_gated_recovery(tmp_path: Path) -> None:
    """Scenario A (FW-02): Orphan PENDING without checkpoint interrupt is marked FAILED on startup; recovery requires lock."""
    cfg = _make_sqlite_settings(tmp_path)

    # Create orphan PENDING request in SQLite with no corresponding checkpoint interrupt
    hitl = HITLManager(db_path=cfg.storage.sqlite_path)
    try:
        orphan = hitl.create_request(
            thread_id="sess-orphan-pending",
            run_id="run-orphan",
            tool_name="execute_shell_command",
            arguments={"command": "echo orphan"},
            tool_call_id="call_orphan_1",
        )
        orphan_id = orphan.id

        # Structural lock gate check: unacquired RuntimeInstanceLock refuses startup recovery
        unheld_lock = RuntimeInstanceLock.from_settings(cfg, instance_id="unheld")
        with pytest.raises(RuntimeError, match="RuntimeInstanceLock must be acquired"):
            await reconcile_on_startup(
                hitl_store=hitl.store,
                current_runtime_id="unheld",
                runtime_lock=unheld_lock,
            )
    finally:
        hitl.close()

    # Starting AgentRuntime acquires lock and marks orphan PENDING -> FAILED
    async with AgentRuntime.open(cfg, instance_id="rt-orphan-clean") as rt:
        assert rt.startup_recovery is not None
        assert rt.startup_recovery.orphan_pending_marked_failed == 1
        cleaned = rt.hitl_manager.get_request(orphan_id, thread_id="sess-orphan-pending")
        assert cleaned is not None
        assert cleaned.status == ApprovalStatus.FAILED
        assert cleaned.execution_error == "missing_active_checkpoint_interrupt"
