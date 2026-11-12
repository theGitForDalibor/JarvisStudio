"""Phase 2 Stage 2 tests: AsyncSqliteSaver PRAGMAs, legacy compatibility, and recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Annotated

import aiosqlite
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import ApprovalDecision, get_hitl_manager
from jarvis_studio.core.persistence import verify_sqlite_backup
from jarvis_studio.core.persistence.async_sqlite import (
    CheckpointCompatibilityError,
    open_managed_sqlite_checkpointer,
    verify_or_isolate_legacy_checkpoints,
)
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.service.session_service import SessionService
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


class _SimpleGraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


class _MockToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_compatible_legacy_db(db_path: Path) -> None:
    """Create valid LangGraph checkpoints/writes tables with 1 serialized checkpoint row."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
            """
        )
        serde = JsonPlusSerializer(pickle_fallback=False)
        cp_type, cp_blob = serde.dumps_typed(
            {
                "v": 1,
                "id": "cp-legacy-1",
                "ts": "2026-01-01T00:00:00+00:00",
                "channel_values": {"messages": []},
                "channel_versions": {},
                "versions_seen": {},
                "pending_sends": [],
            }
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-thread",
                "",
                "cp-legacy-1",
                None,
                cp_type,
                cp_blob,
                json.dumps({"source": "loop", "step": 1}).encode("utf-8"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_p2_cp_01_sqlite_checkpointer_persists_state_across_restart(
    tmp_path: Path,
) -> None:
    """P2-CP-01: LangGraph state persists to SQLite and restores on a fresh checkpointer."""
    db_path = tmp_path / "checkpoint_state.db"

    def _echo_node(state: _SimpleGraphState) -> dict[str, list[BaseMessage]]:
        last_text = state["messages"][-1].content
        return {"messages": [AIMessage(content=f"echo:{last_text}")]}

    def _compile(checkpointer: AsyncSqliteSaver):
        builder = StateGraph(_SimpleGraphState)
        builder.add_node("echo", _echo_node)
        builder.add_edge(START, "echo")
        builder.add_edge("echo", END)
        return builder.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "sess-persist-01"}}

    managed_a = await open_managed_sqlite_checkpointer(db_path)
    try:
        assert managed_a.compatibility.status == "fresh"
        graph_a = _compile(managed_a.saver)
        res_a = await graph_a.ainvoke({"messages": [HumanMessage(content="hello")]}, config=config)
        assert [m.content for m in res_a["messages"]] == ["hello", "echo:hello"]
    finally:
        await managed_a.aclose()

    assert not managed_a.conn._thread.is_alive()

    # Simulated process restart: brand-new connection + saver + compiled graph
    managed_b = await open_managed_sqlite_checkpointer(db_path)
    try:
        assert managed_b.compatibility.status == "compatible"
        assert managed_b.compatibility.checkpoints_rows >= 1
        graph_b = _compile(managed_b.saver)
        snapshot = await graph_b.aget_state(config)
        restored = [m.content for m in snapshot.values["messages"]]
        assert restored == ["hello", "echo:hello"]

        res_b = await graph_b.ainvoke({"messages": [HumanMessage(content="world")]}, config=config)
        assert [m.content for m in res_b["messages"]] == [
            "hello",
            "echo:hello",
            "world",
            "echo:world",
        ]
    finally:
        await managed_b.aclose()


@pytest.mark.asyncio
async def test_p2_cp_02_pragmas_clean_shutdown_and_error_cleanup(tmp_path: Path) -> None:
    """P2-CP-02: Verify explicit PRAGMAs, clean thread exit, and error cleanup."""
    db_path = tmp_path / "pragmas.db"
    managed = await open_managed_sqlite_checkpointer(db_path, busy_timeout_ms=5000)
    try:
        async with managed.conn.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0].lower() == "wal"
        async with managed.conn.execute("PRAGMA busy_timeout") as cur:
            assert int((await cur.fetchone())[0]) == 5000
        async with managed.conn.execute("PRAGMA synchronous") as cur:
            # 1 == NORMAL
            assert int((await cur.fetchone())[0]) == 1
        async with managed.conn.execute("PRAGMA foreign_keys") as cur:
            assert int((await cur.fetchone())[0]) == 1
    finally:
        await managed.aclose()
        await managed.aclose()  # idempotent

    assert not managed.conn._thread.is_alive()

    # Memory checkpointer is also explicitly configurable and shuts down cleanly
    mem_managed = await open_managed_sqlite_checkpointer(":memory:")
    try:
        assert mem_managed.compatibility.status == "memory"
        async with mem_managed.conn.execute("PRAGMA busy_timeout") as cur:
            assert int((await cur.fetchone())[0]) == 5000
    finally:
        await mem_managed.aclose()
    assert not mem_managed.conn._thread.is_alive()


@pytest.mark.asyncio
async def test_p2_cp_02_setup_failure_still_closes_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AsyncSqliteSaver.setup() fails, the background worker thread must still close."""
    opened_conns: list[aiosqlite.Connection] = []
    from jarvis_studio.core.persistence import async_sqlite as mod

    real_open = mod.open_async_checkpoint_connection

    async def _tracking_open(*args, **kwargs):
        conn = await real_open(*args, **kwargs)
        opened_conns.append(conn)
        return conn

    async def _boom(self) -> None:
        raise RuntimeError("simulated setup failure")

    monkeypatch.setattr(mod, "open_async_checkpoint_connection", _tracking_open)
    monkeypatch.setattr(AsyncSqliteSaver, "setup", _boom)

    with pytest.raises(RuntimeError, match="simulated setup failure"):
        await open_managed_sqlite_checkpointer(tmp_path / "fail_setup.db")

    assert len(opened_conns) == 1
    assert not opened_conns[0]._thread.is_alive()


@pytest.mark.asyncio
async def test_p2_cp_03_legacy_compatibility_preserve_vs_dryrun_and_isolate(
    tmp_path: Path,
) -> None:
    """P2-CP-03 (R4): Valid legacy tables stay untouched; incompatible ones backup & archive."""
    # Case A: Compatible legacy schema & payload -> preserved cleanly
    good_db = tmp_path / "good_legacy.db"
    _create_compatible_legacy_db(good_db)
    good_report = verify_or_isolate_legacy_checkpoints(good_db, isolate_incompatible=False)
    assert good_report.status == "compatible"
    assert good_report.checkpoints_rows == 1

    managed_good = await open_managed_sqlite_checkpointer(good_db)
    try:
        tup = await managed_good.saver.aget_tuple({"configurable": {"thread_id": "legacy-thread"}})
        assert tup is not None
        assert tup.checkpoint["id"] == "cp-legacy-1"
    finally:
        await managed_good.aclose()

    # Case B: Missing columns (v0 schema) -> non-destructive dry run first, then backup + archive
    bad_schema_db = tmp_path / "bad_schema.db"
    raw = sqlite3.connect(str(bad_schema_db))
    raw.execute("PRAGMA user_version = 1")
    raw.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT, checkpoint BLOB)")
    raw.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_id TEXT)")
    raw.execute("INSERT INTO checkpoints VALUES ('t-old', 'cp-old', X'00')")
    raw.commit()
    raw.close()

    before_sha = _sha256(bad_schema_db)
    dry_report = verify_or_isolate_legacy_checkpoints(bad_schema_db, isolate_incompatible=False)
    assert dry_report.status == "incompatible"
    assert dry_report.backup_path is None
    assert _sha256(bad_schema_db) == before_sha

    with pytest.raises(CheckpointCompatibilityError):
        await open_managed_sqlite_checkpointer(bad_schema_db, isolate_incompatible=False)
    assert _sha256(bad_schema_db) == before_sha

    # Writable runtime isolates incompatible tables with verified .bak snapshot first
    isolated = await open_managed_sqlite_checkpointer(bad_schema_db, isolate_incompatible=True)
    try:
        assert isolated.compatibility.status == "isolated"
        assert isolated.compatibility.backup_path is not None
        assert isolated.compatibility.backup_path.exists()
        assert verify_sqlite_backup(isolated.compatibility.backup_path, expected_version=1) == 1
        assert any(name.startswith("checkpoints_legacy_") for name in isolated.compatibility.archived_tables)
        assert any(name.startswith("writes_legacy_") for name in isolated.compatibility.archived_tables)

        # Archived rows remain queryable in legacy table and fresh checkpoints table is usable
        async with isolated.conn.execute(
            f'SELECT thread_id FROM "{isolated.compatibility.archived_tables[0]}"'
        ) as cur:
            rows = await cur.fetchall()
            assert len(rows) == 1 and rows[0][0] == "t-old"
    finally:
        await isolated.aclose()

    # Case C: Unknown serialization type ('pickle') -> also isolated safely
    pickle_db = tmp_path / "bad_payload.db"
    _create_compatible_legacy_db(pickle_db)
    raw2 = sqlite3.connect(str(pickle_db))
    raw2.execute("UPDATE checkpoints SET type = 'pickle', checkpoint = X'8004'")
    raw2.commit()
    raw2.close()

    pickle_report = verify_or_isolate_legacy_checkpoints(pickle_db, isolate_incompatible=False)
    assert pickle_report.status == "incompatible"
    assert "Unknown serialization type" in (pickle_report.reason or "")


@pytest.mark.asyncio
async def test_p2_cp_04_orphaned_persisted_interrupt_fails_closed_without_hitl_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-CP-04 (R4): Persisted interrupt without active HITL approval must fail closed."""
    db_path = tmp_path / "hitl_orphan.db"
    app_db_path = tmp_path / "app_sessions.db"

    tool_invoked = False

    def _wrapped_registry() -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(execute_shell_command)
        return reg

    # Patch subprocess execution inside shell_tools so if anything ever leaked past HITL we fail loudly
    async def _fail_if_executed(*args, **kwargs):
        nonlocal tool_invoked
        tool_invoked = True
        raise AssertionError("Sensitive tool executed without active HITL approval!")

    monkeypatch.setattr(
        "jarvis_studio.tools.builtin.shell_tools._execute_shell_command_async",
        _fail_if_executed,
    )

    hitl_mgr = get_hitl_manager()
    hitl_mgr.clear()
    session_svc = SessionService(db_path=str(app_db_path))
    thread_id = "sess-orphan-interrupt"

    managed_a = await open_managed_sqlite_checkpointer(db_path)
    try:
        model_a = _MockToolModel(
            responses=[
                AIMessage(
                    content="Need shell",
                    tool_calls=[
                        {
                            "id": "call_danger_1",
                            "name": "execute_shell_command",
                            "args": {"command": "echo danger"},
                        }
                    ],
                ),
                AIMessage(content="Finished"),
            ]
        )
        graph_a = build_agent_graph(
            model=model_a,
            registry=_wrapped_registry(),
            checkpointer=managed_a.saver,
        )
        svc_a = AgentService(graph=graph_a)
        svc_a.session_service = session_svc
        svc_a.hitl_manager = hitl_mgr
        res_a = await svc_a.invoke_agent(thread_id=thread_id, user_message="run echo danger")
        assert res_a.status == "interrupted"
        assert res_a.pending_approval is not None
        approval_id = res_a.pending_approval["approval_id"]
        assert await svc_a.verify_approval_context(thread_id, approval_id) is True
    finally:
        await managed_a.aclose()

    # Simulated restart in Phase 2 (HITLManager is cleared; checkpoint stays persisted on disk)
    hitl_mgr.clear()
    managed_b = await open_managed_sqlite_checkpointer(db_path)
    try:
        model_b = _MockToolModel(responses=[AIMessage(content="Should never run")])
        graph_b = build_agent_graph(
            model=model_b,
            registry=_wrapped_registry(),
            checkpointer=managed_b.saver,
        )
        svc_b = AgentService(graph=graph_b)
        svc_b.session_service = session_svc
        svc_b.hitl_manager = hitl_mgr

        # Persisted checkpoint still shows interrupted node on disk
        snapshot = await graph_b.aget_state({"configurable": {"thread_id": thread_id}})
        assert "approval_gate" in snapshot.next

        # Fail-closed gates reject orphaned checkpoint approval attempts
        assert await svc_b.verify_approval_context(thread_id, approval_id) is False
        res_b = await svc_b.resume_approval(
            thread_id=thread_id,
            approval_id=approval_id,
            decision=ApprovalDecision(decision="approve"),
        )
        assert res_b.status == "error"
        assert tool_invoked is False
    finally:
        session_svc.close()
        await managed_b.aclose()
