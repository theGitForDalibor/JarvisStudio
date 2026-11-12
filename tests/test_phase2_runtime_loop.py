"""Phase 2 Stage 3 tests: AgentRuntime lifecycle, Event Loop binding, and saver contract."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from jarvis_studio.agent.checkpoints import saver as saver_mod
from jarvis_studio.agent.checkpoints.saver import get_checkpointer
from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import ApprovalDecision
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.persistence import RuntimeInstanceLock, RuntimeLockError
from jarvis_studio.core.persistence.async_sqlite import (
    CheckpointCompatibilityError,
    open_managed_sqlite_checkpointer,
)
from jarvis_studio.core.runtime import AgentRuntime, RuntimeState
from jarvis_studio.service.agent_service import AgentService


def _make_runtime_settings(
    tmp_path: Path,
    *,
    checkpoint_backend: str = "sqlite",
) -> Settings:
    return Settings(
        storage=StorageConfig(
            checkpoint_backend=checkpoint_backend,
            sqlite_path=str(tmp_path / "jarvis_studio.db"),
            workspace_root=str(tmp_path / "workspace"),
        ),
        rag=RAGConfig(
            enabled=True,
            vector_db_path=str(tmp_path / "jarvis_vec.db"),
        ),
    )


async def _invoke_all_entry_points_on_foreign_loop(svc: AgentService) -> list[str]:
    """Run every async AgentService entrypoint inside a separate thread/event loop."""

    def _thread_runner() -> list[str]:
        async def _probe() -> list[str]:
            errors: list[str] = []

            for label, awaitable in (
                ("invoke_agent", svc.invoke_agent("t-1", "hi")),
                ("verify_approval_context", svc.verify_approval_context("t-1", "appr-1")),
                (
                    "resume_approval",
                    svc.resume_approval("t-1", "appr-1", ApprovalDecision(decision="approve")),
                ),
            ):
                try:
                    await awaitable
                except RuntimeError as exc:
                    assert "mismatched event loop" in str(exc)
                    errors.append(label)
                else:
                    raise AssertionError(f"{label} unexpectedly succeeded on foreign loop")

            for label, agen in (
                ("stream_agent", svc.stream_agent("t-1", "hi")),
                (
                    "stream_resume_approval",
                    svc.stream_resume_approval(
                        "t-1", "appr-1", ApprovalDecision(decision="approve")
                    ),
                ),
            ):
                try:
                    await anext(agen)
                except RuntimeError as exc:
                    assert "mismatched event loop" in str(exc)
                    errors.append(label)
                else:
                    raise AssertionError(f"{label} unexpectedly yielded on foreign loop")
                finally:
                    await agen.aclose()

            return errors

        return asyncio.run(_probe())

    return await asyncio.to_thread(_thread_runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "memory"])
async def test_p2_rt_01_cross_loop_invocation_fails_closed(
    tmp_path: Path,
    backend: str,
) -> None:
    """P2-RT-01 (R2): Managed AgentService rejects calls from any foreign event loop."""
    settings = _make_runtime_settings(tmp_path, checkpoint_backend=backend)
    async with AgentRuntime.open(settings) as runtime:
        assert runtime.state == RuntimeState.READY
        assert runtime.bound_loop is asyncio.get_running_loop()

        rejected = await _invoke_all_entry_points_on_foreign_loop(runtime.agent_service)
        assert rejected == [
            "invoke_agent",
            "verify_approval_context",
            "resume_approval",
            "stream_agent",
            "stream_resume_approval",
        ]


@pytest.mark.asyncio
async def test_p2_rt_01_standalone_async_sqlite_service_cannot_bypass_loop_binding(
    tmp_path: Path,
) -> None:
    """P2-RT-01: AsyncSqliteSaver forces strict single-loop binding even without AgentRuntime."""
    managed = await open_managed_sqlite_checkpointer(tmp_path / "standalone.db")
    try:
        graph = build_agent_graph(checkpointer=managed.saver)
        # Even if caller passes strict_loop_binding=False, AsyncSqliteSaver overrides it to True
        svc = AgentService(graph=graph, strict_loop_binding=False)
        await svc.verify_approval_context("nonexistent-thread", "appr-1")

        def _other_loop() -> None:
            async def _call() -> None:
                await svc.verify_approval_context("nonexistent-thread", "appr-1")

            asyncio.run(_call())

        with pytest.raises(RuntimeError, match="mismatched event loop"):
            await asyncio.to_thread(_other_loop)
    finally:
        await managed.aclose()


@pytest.mark.asyncio
async def test_p2_rt_02_multi_loop_hosts_cannot_coexist_on_shared_root_and_handoff_cleanly(
    tmp_path: Path,
) -> None:
    """P2-RT-02: Loop A locks out Loop B on same persistence root; clean shutdown hands off."""
    settings_a = _make_runtime_settings(tmp_path, checkpoint_backend="sqlite")
    settings_b = _make_runtime_settings(tmp_path, checkpoint_backend="memory")

    async with AgentRuntime.open(settings_a, instance_id="rt-loop-a") as rt_a:
        assert rt_a.bound_loop is asyncio.get_running_loop()

        def _loop_b_start_fails() -> None:
            async def _attempt() -> None:
                async with AgentRuntime.open(settings_b, instance_id="rt-loop-b"):
                    raise AssertionError("Second runtime should not acquire lock")

            asyncio.run(_attempt())

        with pytest.raises(RuntimeLockError) as exc_info:
            await asyncio.to_thread(_loop_b_start_fails)
        assert exc_info.value.holder_metadata is not None
        assert exc_info.value.holder_metadata["instance_id"] == "rt-loop-a"

    assert rt_a.state == RuntimeState.CLOSED
    assert rt_a.lock.is_acquired is False

    # After Loop A closes cleanly, Loop B can start a fresh runtime on the same root
    def _loop_b_succeeds() -> str:
        async def _run() -> str:
            async with AgentRuntime.open(settings_b, instance_id="rt-loop-b") as rt_b:
                assert rt_b.state == RuntimeState.READY
                return rt_b.instance_id

        return asyncio.run(_run())

    assert await asyncio.to_thread(_loop_b_succeeds) == "rt-loop-b"


@pytest.mark.asyncio
async def test_runtime_startup_failure_rolls_back_lock_and_state(tmp_path: Path) -> None:
    """If startup fails mid-way (e.g. incompatible legacy DB), lock is cleanly released."""
    db_path = tmp_path / "jarvis_studio.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute("CREATE TABLE checkpoints (thread_id TEXT)")
    raw.commit()
    raw.close()

    settings = _make_runtime_settings(tmp_path, checkpoint_backend="sqlite")
    rt = AgentRuntime(settings, isolate_incompatible_checkpoints=False)
    with pytest.raises(CheckpointCompatibilityError):
        await rt.start()

    assert rt.state == RuntimeState.CLOSED
    assert rt.lock.is_acquired is False

    # Persistence root lock must not be stranded after startup failure
    probe_lock = RuntimeInstanceLock(rt.lock.lock_paths, instance_id="after-fail")
    probe_lock.acquire()
    probe_lock.release()


@pytest.mark.asyncio
async def test_runtime_startup_lock_conflict_reaches_closed_and_preserves_holder(
    tmp_path: Path,
) -> None:
    """P2: Lock conflict in start() transitions contender to CLOSED without harming active holder."""
    from jarvis_studio.core.runtime import get_active_runtime

    settings = _make_runtime_settings(tmp_path, checkpoint_backend="sqlite")
    holder = AgentRuntime(
        settings,
        instance_id="rt-holder",
        register_as_active=True,
        close_global_stores=True,
    )
    await holder.start()
    try:
        # Ensure holder's session store is active
        sess = holder.agent_service.session_service.get_or_create_session("holder-sess")
        assert sess.session_id == "holder-sess"

        contender = AgentRuntime(
            settings,
            instance_id="rt-contender",
            register_as_active=True,
            close_global_stores=True,
        )
        with pytest.raises(RuntimeLockError) as exc_info:
            await contender.start()

        assert exc_info.value.holder_metadata is not None
        assert exc_info.value.holder_metadata["instance_id"] == "rt-holder"

        # Contender must reach terminal CLOSED state with zero partial resources (never STARTING)
        assert contender.state == RuntimeState.CLOSED
        assert contender.lock.is_acquired is False
        assert contender.bound_loop is None
        assert contender._managed_sqlite is None
        assert contender._agent_service is None

        # Retrying start() on the same failed instance is explicitly rejected; shutdown() is safe
        with pytest.raises(RuntimeError, match="create a new AgentRuntime instance"):
            await contender.start()
        await contender.shutdown()

        # Existing holder, its active registration, and its stores remain intact
        assert holder.state == RuntimeState.READY
        assert holder.lock.is_acquired is True
        assert get_active_runtime() is holder
        assert holder.agent_service.session_service.get_session("holder-sess") is not None
    finally:
        await holder.shutdown()

    # Retrying with a fresh AgentRuntime instance succeeds after holder releases
    async with AgentRuntime.open(settings, instance_id="rt-retry-new") as retry_rt:
        assert retry_rt.state == RuntimeState.READY


@pytest.mark.asyncio
async def test_runtime_shutdown_rejects_mismatched_loop(tmp_path: Path) -> None:
    """Closing AgentRuntime from another live event loop fails closed."""
    settings = _make_runtime_settings(tmp_path, checkpoint_backend="memory")
    rt = AgentRuntime(settings)
    await rt.start()
    try:
        def _wrong_loop_shutdown() -> None:
            asyncio.run(rt.shutdown())

        with pytest.raises(RuntimeError, match="mismatched event loop"):
            await asyncio.to_thread(_wrong_loop_shutdown)
    finally:
        await rt.shutdown()


def test_saver_factory_no_global_cache_and_blocks_sync_sqlite() -> None:
    """saver.py no longer caches global checkpointers and rejects sync sqlite mode."""
    assert not hasattr(AgentService, "close")
    assert saver_mod._cached_checkpointer is None

    m1 = get_checkpointer("memory")
    m2 = get_checkpointer("memory")
    assert m1 is not m2
    assert saver_mod._cached_checkpointer is None

    with pytest.raises(RuntimeError, match="async managed lifecycle"):
        get_checkpointer("sqlite")
