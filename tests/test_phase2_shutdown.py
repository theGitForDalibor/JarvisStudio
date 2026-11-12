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
