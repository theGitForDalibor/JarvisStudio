"""Phase 2 Stage 5 tests: FastAPI/CLI runtime lifecycle and Graceful Shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from jarvis_studio.api.app import create_app
from jarvis_studio.cli.main import cli
from jarvis_studio.config import settings as settings_mod
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.persistence import RuntimeInstanceLock
from jarvis_studio.core.runtime import AgentRuntime, RuntimeState, get_active_runtime
from jarvis_studio.service.agent_service import AgentService, get_agent_service


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


def test_p2_shut_01_fastapi_lifespan_owns_sqlite_runtime_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-SHUT-01: FastAPI lifespan opens and cleanly shuts down SQLite AgentRuntime."""
    cfg = _make_sqlite_settings(tmp_path)
    monkeypatch.setattr(settings_mod, "_global_settings", cfg)

    app = create_app()
    assert not hasattr(AgentService, "close")

    with TestClient(app) as client:
        rt = app.state.agent_runtime
        assert isinstance(rt, AgentRuntime)
        assert rt.state == RuntimeState.READY
        assert rt.lock.is_acquired is True
        assert get_active_runtime() is rt
        assert get_agent_service() is rt.agent_service
        worker_thread = rt._managed_sqlite.conn._thread  # type: ignore[union-attr]
        assert worker_thread.is_alive() is True

        resp = client.get("/api/v1/system/health")
        assert resp.status_code == 200

    assert rt.state == RuntimeState.CLOSED
    assert rt.lock.is_acquired is False
    assert get_active_runtime() is None
    assert not worker_thread.is_alive()

    # Successor lock succeeds immediately on the same root
    probe = RuntimeInstanceLock(rt.lock.lock_paths, instance_id="post-fastapi")
    probe.acquire()
    probe.release()


def test_p2_shut_01_cli_chat_runs_under_managed_sqlite_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-SHUT-01: CLI chat command initializes & closes SQLite AgentRuntime cleanly."""
    cfg = _make_sqlite_settings(tmp_path)
    monkeypatch.setattr(settings_mod, "_global_settings", cfg)
    monkeypatch.setattr("jarvis_studio.cli.main.console.input", lambda *args, **kwargs: "exit")

    runner = CliRunner()
    result = runner.invoke(cli, ["chat", "--no-mcp"])
    assert result.exit_code == 0, result.output
    assert get_active_runtime() is None

    probe = RuntimeInstanceLock.from_settings(cfg, instance_id="post-cli")
    probe.acquire()
    probe.release()


@pytest.mark.asyncio
async def test_p2_shut_02_graceful_shutdown_orders_drain_before_resources_and_rejects_new_runs(
    tmp_path: Path,
) -> None:
    """P2-SHUT-02 (R5): shutdown cancels & drains active runs before closing DB/MCP/stores/lock."""
    cfg = _make_sqlite_settings(tmp_path)
    events: list[str] = []
    started_event = asyncio.Event()
    never_set = asyncio.Event()

    class _BlockedGraph:
        def __init__(self, checkpointer: Any) -> None:
            self.checkpointer = checkpointer

        async def aget_state(self, config: dict[str, Any]):
            class _Snap:
                def __init__(self) -> None:
                    self.values: dict[str, Any] = {}
                    self.next: tuple[str, ...] = ()
                    self.tasks: tuple[Any, ...] = ()

            return _Snap()

        async def ainvoke(self, input_state: Any, config: dict[str, Any]):
            events.append("run_entered")
            started_event.set()
            try:
                await never_set.wait()
                return {"messages": [AIMessage(content="unreachable")]}
            finally:
                events.append("task_unwound")

    class _SpyMCP:
        async def disconnect_all(self, registry: Any) -> None:
            events.append("mcp_disconnected")

    class _SpyStore:
        def close(self) -> None:
            events.append("store_closed")

    rt = AgentRuntime(
        cfg,
        register_as_active=True,
        stores=[_SpyStore()],
        drain_timeout=3.0,
    )
    await rt.start()
    rt._mcp_manager = _SpyMCP()

    # Instrument managed checkpointer & lock release to verify strict shutdown order
    managed = rt._managed_sqlite
    assert managed is not None
    orig_aclose = managed.aclose

    async def _wrapped_aclose() -> None:
        await orig_aclose()
        events.append("checkpointer_closed")

    managed.aclose = _wrapped_aclose  # type: ignore[method-assign]

    orig_release = rt.lock.release

    def _wrapped_release() -> None:
        orig_release()
        events.append("lock_released")

    rt.lock.release = _wrapped_release  # type: ignore[method-assign]

    # Swap graph to our deterministic blocking double while preserving checkpointer & hooks
    svc = rt.agent_service
    svc.graph = _BlockedGraph(rt.checkpointer)

    active_task = asyncio.create_task(svc.invoke_agent("sess-shut-02", "long task"))
    await started_event.wait()
    assert rt.active_run_count == 1

    # Shutdown drains active run first, then closes checkpointer -> MCP -> stores -> lock
    shutdown_task = asyncio.create_task(rt.shutdown())
    await asyncio.sleep(0)
    assert rt.state == RuntimeState.SHUTTING_DOWN

    # New execution requests during shutdown fail closed immediately
    with pytest.raises(RuntimeError, match="not accepting new executions"):
        await svc.invoke_agent("sess-shut-new", "too late")

    await shutdown_task
    with pytest.raises(asyncio.CancelledError):
        await active_task

    assert rt.state == RuntimeState.CLOSED
    assert rt.active_run_count == 0
    assert events == [
        "run_entered",
        "task_unwound",
        "checkpointer_closed",
        "mcp_disconnected",
        "store_closed",
        "lock_released",
    ]


@pytest.mark.asyncio
async def test_p2_shut_03_shutdown_collects_errors_without_leaking_connection_or_lock(
    tmp_path: Path,
) -> None:
    """P2-SHUT-03 (R5): Cleanup errors are preserved together without skipping lock/DB close."""
    cfg = _make_sqlite_settings(tmp_path)
    closed_markers: list[str] = []

    class _FailingMCP:
        async def disconnect_all(self, registry: Any) -> None:
            raise RuntimeError("mcp disconnect boom")

    class _FailingStore:
        def close(self) -> None:
            raise OSError("store close boom")

    class _HealthyStore:
        def close(self) -> None:
            closed_markers.append("healthy_store_closed")

    rt = AgentRuntime(
        cfg,
        register_as_active=True,
        stores=[_HealthyStore(), _FailingStore()],
    )
    await rt.start()
    rt._mcp_manager = _FailingMCP()
    worker_thread = rt._managed_sqlite.conn._thread  # type: ignore[union-attr]

    with pytest.raises(BaseExceptionGroup) as exc_info:
        await rt.shutdown()

    exc_msgs = [str(e) for e in exc_info.value.exceptions]
    assert "mcp disconnect boom" in exc_msgs
    assert "store close boom" in exc_msgs

    # Everything else still closed cleanly with zero leaked worker thread or lock
    assert closed_markers == ["healthy_store_closed"]
    assert not worker_thread.is_alive()
    assert rt.lock.is_acquired is False
    assert rt.state == RuntimeState.CLOSED
    assert get_active_runtime() is None

    probe = RuntimeInstanceLock(rt.lock.lock_paths, instance_id="after-error-shutdown")
    probe.acquire()
    probe.release()


@pytest.mark.asyncio
async def test_p2_shut_04_drain_timeout_escalates_preserves_resources_and_retries_cleanly(
    tmp_path: Path,
) -> None:
    """P1: Drain timeout escalates, keeps lock/checkpointer/stores/tasks intact, resists double-cancel, and recovers on retry."""
    from jarvis_studio.core.persistence import RuntimeLockError

    cfg = _make_sqlite_settings(tmp_path)
    started_event = asyncio.Event()
    allow_exit_event = asyncio.Event()
    cancel_count = 0

    class _StubbornGraph:
        def __init__(self, checkpointer: Any) -> None:
            self.checkpointer = checkpointer

        async def aget_state(self, config: dict[str, Any]):
            class _Snap:
                def __init__(self) -> None:
                    self.values: dict[str, Any] = {}
                    self.next: tuple[str, ...] = ()
                    self.tasks: tuple[Any, ...] = ()

            return _Snap()

        async def ainvoke(self, input_state: Any, config: dict[str, Any]):
            nonlocal cancel_count
            started_event.set()
            while not allow_exit_event.is_set():
                try:
                    await allow_exit_event.wait()
                except asyncio.CancelledError:
                    cancel_count += 1
            return {"messages": [AIMessage(content="done after release")]}

    class _SpyStore:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    spy_store = _SpyStore()
    rt = AgentRuntime(
        cfg,
        instance_id="rt-stubborn-owner",
        register_as_active=True,
        stores=[spy_store],
        drain_timeout=0.05,
    )
    await rt.start()
    worker_thread = rt._managed_sqlite.conn._thread  # type: ignore[union-attr]
    svc = rt.agent_service
    svc.graph = _StubbornGraph(rt.checkpointer)

    active_task = asyncio.create_task(svc.invoke_agent("sess-stubborn", "hold"))
    await started_event.wait()
    assert rt.active_run_count == 1

    # 1. First shutdown times out after initial cancel + escalated cancel
    with pytest.raises(TimeoutError, match="remained alive"):
        await rt.shutdown()

    assert cancel_count >= 2  # initial cancel + escalated cancel both delivered
    assert rt.state == RuntimeState.SHUTTING_DOWN
    assert rt.state != RuntimeState.CLOSED
    assert rt.lock.is_acquired is True
    assert spy_store.closed is False
    assert rt.active_run_count == 1
    assert worker_thread.is_alive() is True

    # Checkpointer remains open & usable while task is still alive
    tup = await rt.checkpointer.aget_tuple({"configurable": {"thread_id": "probe-alive"}})
    assert tup is None

    # Second runtime cannot acquire the same persistence root lock
    rt2 = AgentRuntime(cfg, instance_id="rt-contender")
    with pytest.raises(RuntimeLockError):
        await rt2.start()
    assert rt2.state == RuntimeState.CLOSED
    assert rt2.lock.is_acquired is False

    # 2. Cancelling shutdown itself (including double cancellation) must NEVER release lock early
    shut_task_1 = asyncio.create_task(rt.shutdown())
    await asyncio.sleep(0.01)
    shut_task_1.cancel()
    shut_task_1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shut_task_1

    shut_task_2 = asyncio.create_task(rt.shutdown())
    await asyncio.sleep(0.01)
    shut_task_2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shut_task_2

    assert rt.lock.is_acquired is True
    assert rt.state == RuntimeState.SHUTTING_DOWN
    assert spy_store.closed is False
    assert rt.active_run_count == 1
    assert worker_thread.is_alive() is True

    # 3. Once the stubborn task genuinely exits, retrying shutdown completes full teardown
    allow_exit_event.set()
    result = await active_task
    assert result.status == "completed"
    assert rt.active_run_count == 0

    await rt.shutdown()
    assert rt.state == RuntimeState.CLOSED
    assert rt.lock.is_acquired is False
    assert spy_store.closed is True
    assert not worker_thread.is_alive()
    assert get_active_runtime() is None

    # Successor runtime can now acquire lock cleanly
    async with AgentRuntime.open(cfg, instance_id="rt-successor") as rt3:
        assert rt3.state == RuntimeState.READY


@pytest.mark.asyncio
async def test_p2_shut_05_worker_thread_alive_prevents_closed_state_and_lock_release(
    tmp_path: Path,
) -> None:
    """P2: Surviving aiosqlite worker thread fails shutdown and retains lock until exit."""
    import threading

    from jarvis_studio.core.persistence import RuntimeLockError
    from jarvis_studio.core.persistence.async_sqlite import (
        CheckpointCloseError,
        close_async_checkpoint_connection,
    )

    cfg = _make_sqlite_settings(tmp_path)
    rt = AgentRuntime(cfg, instance_id="rt-stuck-thread", register_as_active=True)
    await rt.start()
    managed = rt._managed_sqlite
    assert managed is not None

    release_thread = threading.Event()
    stuck_thread = threading.Thread(target=release_thread.wait, daemon=True)
    stuck_thread.start()

    # Cleanly stop the real aiosqlite worker first, then expose an alive thread on conn._thread
    await managed.conn.close()
    managed.conn._thread = stuck_thread  # type: ignore[attr-defined]

    async def _noop_close() -> None:
        return None

    managed.conn.close = _noop_close  # type: ignore[method-assign]
    managed.aclose = lambda: close_async_checkpoint_connection(  # type: ignore[method-assign]
        managed.conn, join_timeout=0.05
    )

    try:
        with pytest.raises(CheckpointCloseError, match="remained alive"):
            await rt.shutdown()

        assert rt.state == RuntimeState.SHUTTING_DOWN
        assert rt.state != RuntimeState.CLOSED
        assert rt.lock.is_acquired is True
        assert rt._managed_sqlite is managed
        assert managed._closed is False

        # Contender cannot acquire the persistence root lock while thread is still alive
        rt2 = AgentRuntime(cfg, instance_id="rt-contender-thread")
        with pytest.raises(RuntimeLockError):
            await rt2.start()
    finally:
        release_thread.set()
        stuck_thread.join(timeout=2.0)

    # Restore real ManagedSqliteCheckpointer.aclose semantics & retry shutdown cleanly
    from jarvis_studio.core.persistence.async_sqlite import ManagedSqliteCheckpointer

    managed.aclose = ManagedSqliteCheckpointer.aclose.__get__(managed, ManagedSqliteCheckpointer)  # type: ignore[method-assign]
    await rt.shutdown()
    assert rt.state == RuntimeState.CLOSED
    assert rt.lock.is_acquired is False
    assert rt._managed_sqlite is None
    assert get_active_runtime() is None


@pytest.mark.asyncio
async def test_p2_service_resolution_prefers_active_runtime_over_prior_default_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: Pre-existing default fallback AgentService never shadows an active AgentRuntime."""
    from jarvis_studio.service import agent_service as agent_svc_mod

    assert agent_svc_mod._global_agent_service is None
    assert agent_svc_mod._default_agent_service is None

    # 1. Pre-runtime call initializes lazy fallback singleton (not _global_agent_service override)
    default_svc = get_agent_service()
    assert agent_svc_mod._default_agent_service is default_svc
    assert agent_svc_mod._global_agent_service is None
    assert get_agent_service() is default_svc

    # 2. Starting an active AgentRuntime must take precedence over prior fallback
    cfg = _make_sqlite_settings(tmp_path)
    async with AgentRuntime.open(
        cfg,
        register_as_active=True,
        close_global_stores=True,
    ) as rt:
        resolved = get_agent_service()
        assert resolved is rt.agent_service
        assert resolved is not default_svc

        # 3. Explicit test override (_global_agent_service) still overrides when set
        sentinel_override = AgentService()
        monkeypatch.setattr(agent_svc_mod, "_global_agent_service", sentinel_override)
        assert get_agent_service() is sentinel_override
        monkeypatch.setattr(agent_svc_mod, "_global_agent_service", None)
        assert get_agent_service() is rt.agent_service

    # After runtime shutdown with close_global_stores=True, stale fallback is also detached
    assert agent_svc_mod._default_agent_service is None


@pytest.mark.asyncio
async def test_p2_mcp_runtime_and_cli_register_builtins_once_and_disconnect_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: AgentRuntime + CLI chat --mcp register builtin tools once and disconnect MCP cleanly."""
    import sys

    from jarvis_studio.tools.mcp import client as mcp_client_mod
    from jarvis_studio.tools.mcp.client import MCPClientManager, MCPServerConfig
    from jarvis_studio.tools.registry import ToolRegistry
    from tests.test_mcp import MOCK_MCP_SERVER_SCRIPT

    cfg = _make_sqlite_settings(tmp_path)
    registry = ToolRegistry()
    reg_counts: dict[str, int] = {}
    orig_register = registry.register

    def _counting_register(tool_obj, *args, **kwargs):
        reg_counts[tool_obj.name] = reg_counts.get(tool_obj.name, 0) + 1
        return orig_register(tool_obj, *args, **kwargs)

    registry.register = _counting_register  # type: ignore[method-assign]

    mcp_mgr = MCPClientManager(config_path="nonexistent.json")
    mcp_mgr.server_configs["live_srv"] = MCPServerConfig(
        command=sys.executable,
        args=["-c", MOCK_MCP_SERVER_SCRIPT],
        enabled=True,
        high_risk_tools=["dangerous_clean"],
    )
    monkeypatch.setattr(mcp_client_mod, "_global_mcp_manager", mcp_mgr)

    async with AgentRuntime.open(
        cfg,
        register_as_active=True,
        manage_mcp=True,
        tool_registry=registry,
        close_global_stores=True,
    ) as rt:
        assert rt.state == RuntimeState.READY
        # Builtin tools registered strictly once
        assert reg_counts["read_file"] == 1
        assert reg_counts["execute_shell_command"] == 1
        # Idempotent re-invocation does not re-register
        from jarvis_studio.tools.builtin import register_builtin_tools

        register_builtin_tools(registry)
        assert reg_counts["read_file"] == 1
        assert reg_counts["execute_shell_command"] == 1

        # MCP tools registered & live
        assert registry.get("live_srv_calculate_sum") is not None
        assert registry.get("live_srv_dangerous_clean") is not None
        assert reg_counts["live_srv_calculate_sum"] == 1
        assert mcp_mgr.connections["live_srv"].is_connected is True

    # Shutdown disconnected MCP server and unregistered MCP tools, while preserving builtins
    assert mcp_mgr.connections["live_srv"].is_connected is False
    assert registry.get("live_srv_calculate_sum") is None
    assert registry.get("live_srv_dangerous_clean") is None
    assert registry.get("read_file") is not None


def test_p2_cli_chat_mcp_registers_builtins_once_and_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: CLI `chat --mcp` delegates tool registration to AgentRuntime without duplicates."""
    import sys

    from jarvis_studio.tools import registry as reg_mod
    from jarvis_studio.tools.mcp import client as mcp_client_mod
    from jarvis_studio.tools.mcp.client import MCPClientManager, MCPServerConfig
    from jarvis_studio.tools.registry import ToolRegistry
    from tests.test_mcp import MOCK_MCP_SERVER_SCRIPT

    cfg = _make_sqlite_settings(tmp_path)
    cli_registry = ToolRegistry()
    cli_counts: dict[str, int] = {}
    orig_cli_register = cli_registry.register

    def _counting_cli_register(tool_obj, *args, **kwargs):
        cli_counts[tool_obj.name] = cli_counts.get(tool_obj.name, 0) + 1
        return orig_cli_register(tool_obj, *args, **kwargs)

    cli_registry.register = _counting_cli_register  # type: ignore[method-assign]
    cli_mcp_mgr = MCPClientManager(config_path="nonexistent.json")
    cli_mcp_mgr.server_configs["live_srv"] = MCPServerConfig(
        command=sys.executable,
        args=["-c", MOCK_MCP_SERVER_SCRIPT],
        enabled=True,
    )

    monkeypatch.setattr(settings_mod, "_global_settings", cfg)
    monkeypatch.setattr(reg_mod, "_global_registry", cli_registry)
    monkeypatch.setattr(mcp_client_mod, "_global_mcp_manager", cli_mcp_mgr)
    monkeypatch.setattr("jarvis_studio.cli.main.console.input", lambda *args, **kwargs: "exit")

    runner = CliRunner()
    result = runner.invoke(cli, ["chat", "--mcp"])
    assert result.exit_code == 0, result.output
    assert cli_counts["read_file"] == 1
    assert cli_counts["execute_shell_command"] == 1
    assert cli_counts["live_srv_calculate_sum"] == 1
    assert cli_mcp_mgr.connections["live_srv"].is_connected is False
    assert cli_registry.get("live_srv_calculate_sum") is None
    assert get_active_runtime() is None
