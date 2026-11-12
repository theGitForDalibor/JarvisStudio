"""Unified AgentRuntime lifecycle container (Phase 2 Stages 3 & 5).

Owns process-wide runtime infrastructure resources for one active host:
- Unique bound main Event Loop (`bound_loop`)
- `RuntimeInstanceLock` for shared persistence roots
- Managed checkpointer (`AsyncSqliteSaver` + `aiosqlite.Connection` or `MemorySaver`)
- `AgentService` bound strictly to `bound_loop` (without exposing `AgentService.close`)
- Active run task tracking, MCP disconnect, Store cleanup, and Graceful Shutdown
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from jarvis_studio.agent.checkpoints.saver import create_memory_checkpointer
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.persistence.async_sqlite import (
    CheckpointCompatibilityReport,
    ManagedSqliteCheckpointer,
    open_managed_sqlite_checkpointer,
)
from jarvis_studio.core.persistence.runtime_lock import RuntimeInstanceLock

if TYPE_CHECKING:
    from jarvis_studio.config.settings import Settings
    from jarvis_studio.service.agent_service import AgentService
    from jarvis_studio.tools.registry import ToolRegistry


class RuntimeState(StrEnum):
    """Lifecycle states of an `AgentRuntime` instance."""

    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"


_active_runtime: AgentRuntime | None = None


def get_active_runtime() -> AgentRuntime | None:
    """Return the process-wide registered `AgentRuntime`, if any."""
    return _active_runtime


def set_active_runtime(runtime: AgentRuntime | None) -> None:
    """Set or clear the registered active `AgentRuntime` reference."""
    global _active_runtime
    _active_runtime = runtime


def _run_cleanup_action(errors: list[BaseException], action: Callable[[], object]) -> None:
    try:
        action()
    except BaseException as exc:  # noqa: BLE001 - collected and surfaced after all steps finish
        errors.append(exc)


async def _run_async_cleanup_action(
    errors: list[BaseException],
    coro_fn: Callable[[], object],
) -> None:
    try:
        await coro_fn()  # type: ignore[misc]
    except BaseException as exc:  # noqa: BLE001 - collected and surfaced after all steps finish
        errors.append(exc)


def _pop_loaded_attr(module_name: str, attr_name: str) -> object | None:
    module = sys.modules.get(module_name)
    if module is None:
        return None
    value = getattr(module, attr_name, None)
    setattr(module, attr_name, None)
    return value


def _collect_loaded_global_stores() -> list[object]:
    """Detach and collect currently loaded module-level persistent store singletons."""
    session_svc = _pop_loaded_attr(
        "jarvis_studio.service.session_service", "_global_session_service"
    )
    mem_mgr = _pop_loaded_attr("jarvis_studio.memory.manager", "_global_memory_manager")
    trace_store = _pop_loaded_attr(
        "jarvis_studio.core.observability.trace_logger", "_global_trace_store"
    )
    rag_store = _pop_loaded_attr("jarvis_studio.rag.store", "_global_vec_store")

    stores: list[object] = []
    for candidate in (
        getattr(mem_mgr, "store", None),
        trace_store,
        session_svc,
        rag_store,
    ):
        if candidate is not None and callable(getattr(candidate, "close", None)):
            stores.append(candidate)
    return stores


class AgentRuntime:
    """Single-loop host managing persistence lock, checkpointer, tasks, MCP, and stores."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        instance_id: str | None = None,
        isolate_incompatible_checkpoints: bool = True,
        strict_loop_binding: bool = True,
        register_as_active: bool = False,
        manage_mcp: bool = False,
        tool_registry: ToolRegistry | None = None,
        stores: Sequence[object] = (),
        close_global_stores: bool | None = None,
        drain_timeout: float = 5.0,
    ) -> None:
        if settings is None:
            from jarvis_studio.config.settings import get_settings

            settings = get_settings()

        self._settings = settings
        self._lock = RuntimeInstanceLock.from_settings(settings, instance_id=instance_id)
        self._isolate_incompatible_checkpoints = isolate_incompatible_checkpoints
        self._strict_loop_binding = strict_loop_binding
        self._register_as_active = register_as_active
        self._manage_mcp = manage_mcp
        self._tool_registry = tool_registry
        self._owned_stores: list[object] = list(stores)
        self._close_global_stores = (
            register_as_active if close_global_stores is None else close_global_stores
        )
        self._drain_timeout = drain_timeout

        self._state: RuntimeState = RuntimeState.CREATED
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._managed_sqlite: ManagedSqliteCheckpointer | None = None
        self._checkpointer: BaseCheckpointSaver | None = None
        self._agent_service: AgentService | None = None
        self._mcp_manager: Any | None = None
        self._active_tasks: dict[asyncio.Task[Any], int] = {}

    @property
    def state(self) -> RuntimeState:
        """Current runtime lifecycle state."""
        return self._state

    @property
    def instance_id(self) -> str:
        """Unique identifier shared with `RuntimeInstanceLock`."""
        return self._lock.instance_id

    @property
    def settings(self) -> Settings:
        """Settings snapshot used to configure this runtime."""
        return self._settings

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """The single main Event Loop bound at `start()`, or `None` before startup."""
        return self._bound_loop

    @property
    def lock(self) -> RuntimeInstanceLock:
        """Persistence root lock owned by this runtime."""
        return self._lock

    @property
    def checkpointer(self) -> BaseCheckpointSaver:
        """Active checkpointer managed by this runtime."""
        if self._checkpointer is None:
            raise RuntimeError("AgentRuntime checkpointer is not initialized")
        return self._checkpointer

    @property
    def checkpoint_compatibility(self) -> CheckpointCompatibilityReport | None:
        """Legacy compatibility report when running with SQLite checkpointing."""
        if self._managed_sqlite is None:
            return None
        return self._managed_sqlite.compatibility

    @property
    def active_run_count(self) -> int:
        """Number of asyncio tasks currently executing runs on this runtime."""
        return len(self._active_tasks)

    @property
    def tool_registry(self) -> ToolRegistry | None:
        """Tool registry associated with this runtime, if initialized."""
        return self._tool_registry

    @property
    def agent_service(self) -> AgentService:
        """Bound `AgentService` orchestrated by this runtime."""
        if self._agent_service is None or self._state != RuntimeState.READY:
            raise RuntimeError(
                f"AgentRuntime is not ready (state={self._state.value}); cannot access AgentService"
            )
        return self._agent_service

    def _begin_run(self) -> asyncio.Task[Any] | None:
        """Gate and register an active run task while `READY`, failing closed otherwise."""
        if self._state != RuntimeState.READY:
            raise RuntimeError(
                f"AgentRuntime is not accepting new executions (state={self._state.value})"
            )
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[task] = self._active_tasks.get(task, 0) + 1
        return task

    def _end_run(self, task: asyncio.Task[Any] | None) -> None:
        """Unregister an active run task when execution finishes or unwinds."""
        if task is None or task not in self._active_tasks:
            return
        remaining = self._active_tasks[task] - 1
        if remaining <= 0:
            self._active_tasks.pop(task, None)
        else:
            self._active_tasks[task] = remaining

    async def start(self) -> AgentRuntime:
        """Bind main loop, acquire lock, open checkpointer, connect MCP, and start service."""
        if self._state != RuntimeState.CREATED:
            raise RuntimeError(
                f"AgentRuntime cannot start from state {self._state.value!r}"
            )

        loop = asyncio.get_running_loop()
        self._bound_loop = loop
        self._state = RuntimeState.STARTING

        self._lock.acquire()
        try:
            backend = self._settings.storage.checkpoint_backend.strip().lower()
            if backend == "sqlite":
                self._managed_sqlite = await open_managed_sqlite_checkpointer(
                    self._settings.storage.sqlite_path,
                    isolate_incompatible=self._isolate_incompatible_checkpoints,
                )
                saver: BaseCheckpointSaver = self._managed_sqlite.saver
            elif backend == "memory":
                saver = create_memory_checkpointer()
            else:
                raise ValueError(f"Unsupported checkpoint_backend: {backend!r}")

            registry = self._tool_registry
            if self._manage_mcp:
                from jarvis_studio.tools.builtin import register_builtin_tools
                from jarvis_studio.tools.mcp.client import get_mcp_manager
                from jarvis_studio.tools.registry import get_default_registry

                if registry is None:
                    registry = get_default_registry()
                    self._tool_registry = registry
                register_builtin_tools(registry)
                mcp_mgr = get_mcp_manager()
                self._mcp_manager = mcp_mgr
                try:
                    await mcp_mgr.connect_and_register_all(registry)
                except Exception as exc:  # noqa: BLE001 - preserve MCP graceful degradation
                    logger.error(f"Error during MCP servers initialization: {exc}")

            from jarvis_studio.service.agent_service import AgentService

            self._checkpointer = saver
            self._agent_service = AgentService(
                registry=registry,
                checkpointer=saver,
                bound_loop=loop,
                strict_loop_binding=self._strict_loop_binding,
                run_begin_hook=self._begin_run,
                run_end_hook=self._end_run,
            )
            if self._register_as_active:
                set_active_runtime(self)
            self._state = RuntimeState.READY
            logger.info(
                f"AgentRuntime started (instance_id={self.instance_id}, backend={backend})"
            )
            return self
        except BaseException:
            await self.shutdown()
            raise

    async def _drain_active_runs(self, errors: list[BaseException]) -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in list(self._active_tasks)
            if task is not current and not task.done()
        ]
        if not pending:
            return

        for task in pending:
            task.cancel()

        try:
            _, still_pending = await asyncio.wait(pending, timeout=self._drain_timeout)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            return

        if still_pending:
            errors.append(
                TimeoutError(
                    f"{len(still_pending)} active run task(s) did not terminate within "
                    f"{self._drain_timeout}s during runtime shutdown"
                )
            )

    def _close_stores(self, errors: list[BaseException]) -> None:
        candidates: list[object] = list(reversed(self._owned_stores))
        if self._agent_service is not None:
            candidates.append(getattr(self._agent_service, "session_service", None))
        if self._close_global_stores:
            candidates.extend(_collect_loaded_global_stores())

        seen_ids: set[int] = set()
        for store in candidates:
            if store is None or id(store) in seen_ids:
                continue
            close_fn = getattr(store, "close", None)
            if not callable(close_fn):
                continue
            seen_ids.add(id(store))
            _run_cleanup_action(errors, close_fn)

    async def shutdown(self) -> None:
        """Gracefully drain runs, close checkpointer/MCP/stores, and release root lock."""
        if self._state == RuntimeState.CLOSED:
            return

        running_loop = asyncio.get_running_loop()
        if (
            self._bound_loop is not None
            and not self._bound_loop.is_closed()
            and running_loop is not self._bound_loop
        ):
            raise RuntimeError(
                "AgentRuntime.shutdown() invoked on mismatched event loop: "
                f"bound_loop={id(self._bound_loop)}, current_loop={id(running_loop)}"
            )

        self._state = RuntimeState.SHUTTING_DOWN
        errors: list[BaseException] = []

        # 1. Stop new runs & drain active tasks BEFORE tearing down Checkpointer/Stores
        await self._drain_active_runs(errors)

        # 2. Detach global active runtime reference
        if self._register_as_active and get_active_runtime() is self:
            _run_cleanup_action(errors, lambda: set_active_runtime(None))

        # 3. Close AsyncSqliteSaver connection & stop worker thread
        if self._managed_sqlite is not None:
            managed = self._managed_sqlite
            self._managed_sqlite = None
            await _run_async_cleanup_action(errors, managed.aclose)

        # 4. Disconnect external MCP sessions
        if self._mcp_manager is not None:
            mcp_mgr = self._mcp_manager
            registry = self._tool_registry
            self._mcp_manager = None
            await _run_async_cleanup_action(
                errors,
                lambda mgr=mcp_mgr, reg=registry: mgr.disconnect_all(reg),
            )

        # 5. Close owned & loaded persistent stores
        self._close_stores(errors)

        self._agent_service = None
        self._checkpointer = None
        self._active_tasks.clear()

        # 6. Release OS persistence root lock last
        _run_cleanup_action(errors, self._lock.release)
        self._state = RuntimeState.CLOSED
        logger.info(f"AgentRuntime closed (instance_id={self.instance_id})")

        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise BaseExceptionGroup(
                "Multiple errors occurred during AgentRuntime shutdown",
                errors,
            )

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        settings: Settings | None = None,
        *,
        instance_id: str | None = None,
        isolate_incompatible_checkpoints: bool = True,
        strict_loop_binding: bool = True,
        register_as_active: bool = False,
        manage_mcp: bool = False,
        tool_registry: ToolRegistry | None = None,
        stores: Sequence[object] = (),
        close_global_stores: bool | None = None,
        drain_timeout: float = 5.0,
    ) -> AsyncIterator[AgentRuntime]:
        """Async context manager that starts an `AgentRuntime` and always shuts it down."""
        runtime = cls(
            settings,
            instance_id=instance_id,
            isolate_incompatible_checkpoints=isolate_incompatible_checkpoints,
            strict_loop_binding=strict_loop_binding,
            register_as_active=register_as_active,
            manage_mcp=manage_mcp,
            tool_registry=tool_registry,
            stores=stores,
            close_global_stores=close_global_stores,
            drain_timeout=drain_timeout,
        )
        await runtime.start()
        try:
            yield runtime
        finally:
            await runtime.shutdown()
