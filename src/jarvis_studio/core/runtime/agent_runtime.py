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
    from jarvis_studio.agent.hitl.manager import HITLManager
    from jarvis_studio.config.settings import Settings
    from jarvis_studio.core.persistence.recovery import StartupRecoveryReport
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


def _run_cleanup_action(errors: list[Exception], action: Callable[[], object]) -> None:
    try:
        action()
    except Exception as exc:
        errors.append(exc)


_GlobalStoreBinding = tuple[object, str, object, str | None, object]


def _get_loaded_attr(module_name: str, attr_name: str) -> tuple[object, object] | None:
    module = sys.modules.get(module_name)
    if module is None:
        return None
    value = getattr(module, attr_name, None)
    if value is None:
        return None
    return module, value


def _clear_global_store_binding(binding: _GlobalStoreBinding) -> None:
    """Clear a module global binding only if both holder and nested store still match."""
    module, attr_name, holder, store_attr, expected_store = binding
    current_holder = getattr(module, attr_name, None)
    if current_holder is not holder:
        return
    if store_attr is not None and getattr(current_holder, store_attr, None) is not expected_store:
        return
    setattr(module, attr_name, None)


def _collect_loaded_global_stores() -> list[tuple[object, list[_GlobalStoreBinding]]]:
    """Collect loaded global persistent stores paired with their module bindings.

    Module globals are NOT cleared upfront; callers clear bindings only after the
    underlying store closes cleanly.
    """
    specs: tuple[tuple[str, str, str | None], ...] = (
        ("jarvis_studio.service.agent_service", "_default_agent_service", "session_service"),
        ("jarvis_studio.agent.hitl.manager", "_default_hitl_manager", None),
        ("jarvis_studio.memory.manager", "_global_memory_manager", "store"),
        ("jarvis_studio.core.observability.trace_logger", "_global_trace_store", None),
        ("jarvis_studio.service.session_service", "_global_session_service", None),
        ("jarvis_studio.rag.store", "_global_vec_store", None),
    )

    stores_by_id: dict[int, tuple[object, list[_GlobalStoreBinding]]] = {}
    for module_name, attr_name, store_attr in specs:
        loaded = _get_loaded_attr(module_name, attr_name)
        if loaded is None:
            continue
        module, holder = loaded
        candidate = getattr(holder, store_attr, None) if store_attr is not None else holder
        if candidate is None or not callable(getattr(candidate, "close", None)):
            continue
        store_id = id(candidate)
        if store_id not in stores_by_id:
            stores_by_id[store_id] = (candidate, [])
        stores_by_id[store_id][1].append((module, attr_name, holder, store_attr, candidate))

    return list(stores_by_id.values())


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
        hitl_manager: HITLManager | None = None,
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
        self._injected_hitl_manager = hitl_manager
        self._owned_stores: list[object] = list(stores)
        self._close_global_stores = (
            register_as_active if close_global_stores is None else close_global_stores
        )
        self._drain_timeout = drain_timeout

        self._state: RuntimeState = RuntimeState.CREATED
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._managed_sqlite: ManagedSqliteCheckpointer | None = None
        self._checkpointer: BaseCheckpointSaver | None = None
        self._hitl_manager: HITLManager | None = None
        self._recovery_report: StartupRecoveryReport | None = None
        self._agent_service: AgentService | None = None
        self._mcp_manager: Any | None = None
        self._active_tasks: dict[asyncio.Task[Any], int] = {}
        self._closed_store_ids: set[int] = set()

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
    def hitl_manager(self) -> HITLManager:
        """Persistent HITL manager owned by this runtime."""
        if self._hitl_manager is None:
            raise RuntimeError("AgentRuntime HITLManager is not initialized")
        return self._hitl_manager

    @property
    def startup_recovery(self) -> StartupRecoveryReport | None:
        """Result of the lock-gated startup reconciliation run during `start()`."""
        return self._recovery_report

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
        """Bind main loop, acquire lock, open checkpointer, connect MCP, and start service.

        `AgentRuntime` instances are single-use: if startup fails, any partially initialized
        resources are rolled back and the runtime transitions to `RuntimeState.CLOSED`.
        Retry by constructing a fresh `AgentRuntime` instance.
        """
        if self._state != RuntimeState.CREATED:
            raise RuntimeError(
                f"AgentRuntime cannot start from state {self._state.value!r}; "
                "create a new AgentRuntime instance to retry"
            )

        loop = asyncio.get_running_loop()
        self._bound_loop = loop
        self._state = RuntimeState.STARTING

        try:
            self._lock.acquire()
        except BaseException:
            # Lock was never acquired; do not disturb any active holder, global stores,
            # or lock files. Transition to terminal CLOSED state.
            self._bound_loop = None
            self._state = RuntimeState.CLOSED
            raise

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
            if self._manage_mcp or self._register_as_active:
                from jarvis_studio.tools.builtin import register_builtin_tools
                from jarvis_studio.tools.registry import get_default_registry

                if registry is None:
                    registry = get_default_registry()
                    self._tool_registry = registry
                register_builtin_tools(registry)

            if self._manage_mcp:
                from jarvis_studio.tools.mcp.client import get_mcp_manager

                mcp_mgr = get_mcp_manager()
                self._mcp_manager = mcp_mgr
                try:
                    await mcp_mgr.connect_and_register_all(registry)
                except Exception as exc:
                    logger.error(f"Error during MCP servers initialization: {exc}")

            from jarvis_studio.agent.hitl.manager import HITLManager
            from jarvis_studio.core.persistence.recovery import reconcile_on_startup
            from jarvis_studio.service.agent_service import AgentService

            if self._injected_hitl_manager is not None:
                hitl_mgr = self._injected_hitl_manager
                hitl_mgr.runtime_instance_id = self.instance_id
            else:
                hitl_mgr = HITLManager(
                    db_path=self._settings.storage.sqlite_path,
                    runtime_instance_id=self.instance_id,
                )
            self._hitl_manager = hitl_mgr
            self._recovery_report = await reconcile_on_startup(
                hitl_store=hitl_mgr.store,
                current_runtime_id=self.instance_id,
                checkpointer=saver,
                check_checkpoints=(backend == "sqlite"),
                runtime_lock=self._lock,
            )

            self._checkpointer = saver
            self._agent_service = AgentService(
                registry=registry,
                checkpointer=saver,
                hitl_manager=hitl_mgr,
                bound_loop=loop,
                strict_loop_binding=self._strict_loop_binding,
                run_begin_hook=self._begin_run,
                run_end_hook=self._end_run,
            )
            self._state = RuntimeState.READY
            if self._register_as_active:
                set_active_runtime(self)
            logger.info(
                f"AgentRuntime started (instance_id={self.instance_id}, backend={backend})"
            )
            return self
        except BaseException as exc:
            try:
                await self.shutdown()
            except BaseException as shutdown_exc:
                exc.add_note(
                    f"Startup rollback shutdown failed "
                    f"({type(shutdown_exc).__name__}: {shutdown_exc})"
                )
            raise

    def _live_active_tasks(self) -> list[asyncio.Task[Any]]:
        current = asyncio.current_task()
        return [
            task
            for task in list(self._active_tasks)
            if task is not current and not task.done()
        ]

    async def _drain_active_runs(self) -> None:
        """Cancel and drain active tasks; escalate once on timeout, fail closed if still live."""
        pending = self._live_active_tasks()
        if not pending:
            return

        timeout = max(0.0, self._drain_timeout)
        for task in pending:
            task.cancel("AgentRuntime shutdown requested")

        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        still_live = [task for task in still_pending if not task.done()]
        if not still_live:
            return

        escalation_timeout = min(timeout, 0.5) if timeout > 0 else 0.0
        logger.warning(
            f"{len(still_live)} active run task(s) did not terminate within {timeout}s; "
            f"escalating cancellation (grace={escalation_timeout}s)"
        )
        for task in still_live:
            task.cancel("AgentRuntime escalated shutdown cancellation")

        _, remaining = await asyncio.wait(still_live, timeout=escalation_timeout)
        remaining_live = [task for task in remaining if not task.done()]
        if remaining_live:
            task_names = ", ".join(task.get_name() for task in remaining_live)
            msg = (
                f"{len(remaining_live)} active run task(s) remained alive after drain "
                f"({timeout}s) and escalated cancellation ({escalation_timeout}s): [{task_names}]"
            )
            logger.error(msg)
            raise TimeoutError(msg)

    def _close_stores(self, errors: list[Exception]) -> bool:
        """Close owned, service, and loaded global stores; return True iff all succeeded."""
        owned_ids = {id(store) for store in self._owned_stores}
        candidates: list[tuple[object, list[_GlobalStoreBinding]]] = [
            (store, []) for store in reversed(self._owned_stores)
        ]
        if self._agent_service is not None:
            svc_store = getattr(self._agent_service, "session_service", None)
            if svc_store is not None:
                candidates.append((svc_store, []))
        if self._hitl_manager is not None:
            candidates.append((self._hitl_manager, []))
        if self._close_global_stores:
            candidates.extend(_collect_loaded_global_stores())

        ordered_entries: list[tuple[object, list[_GlobalStoreBinding]]] = []
        entry_by_id: dict[int, tuple[object, list[_GlobalStoreBinding]]] = {}
        for store, bindings in candidates:
            if store is None:
                continue
            close_fn = getattr(store, "close", None)
            if not callable(close_fn):
                continue
            store_id = id(store)
            if store_id in self._closed_store_ids:
                for binding in bindings:
                    _clear_global_store_binding(binding)
                continue
            existing = entry_by_id.get(store_id)
            if existing is None:
                entry = (store, list(bindings))
                entry_by_id[store_id] = entry
                ordered_entries.append(entry)
            else:
                existing[1].extend(bindings)

        closed_owned_ids: set[int] = set()
        all_closed = True
        for store, bindings in ordered_entries:
            store_id = id(store)
            try:
                store.close()  # type: ignore[attr-defined]
            except Exception as exc:
                all_closed = False
                errors.append(exc)
            else:
                self._closed_store_ids.add(store_id)
                if store_id in owned_ids:
                    closed_owned_ids.add(store_id)
                for binding in bindings:
                    _clear_global_store_binding(binding)

        if closed_owned_ids:
            self._owned_stores = [
                store for store in self._owned_stores if id(store) not in closed_owned_ids
            ]

        return all_closed

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

        # 1. Stop new runs & confirm all active tasks terminated BEFORE tearing down resources.
        # Any TimeoutError or CancelledError aborts here, retaining lock & resources for retry.
        await self._drain_active_runs()

        errors: list[Exception] = []

        # 2. Close AsyncSqliteSaver connection & verify worker thread exited cleanly
        checkpointer_closed = True
        if self._managed_sqlite is not None:
            managed = self._managed_sqlite
            try:
                await managed.aclose()
            except Exception as exc:
                checkpointer_closed = False
                errors.append(exc)
            else:
                self._managed_sqlite = None
                self._checkpointer = None
        else:
            self._checkpointer = None

        # 3. Disconnect external MCP sessions; retain manager on failure for retry
        mcp_disconnected = True
        if self._mcp_manager is not None:
            mcp_mgr = self._mcp_manager
            registry = self._tool_registry
            try:
                await mcp_mgr.disconnect_all(registry)
            except Exception as exc:
                mcp_disconnected = False
                errors.append(exc)
            else:
                self._mcp_manager = None

        # 4. Close owned & loaded persistent stores; retain failing stores & globals for retry
        stores_closed = self._close_stores(errors)

        # 5. Detach runtime & release OS lock ONLY when all resources closed cleanly
        if checkpointer_closed and mcp_disconnected and stores_closed:
            self._agent_service = None
            self._hitl_manager = None
            self._active_tasks.clear()
            if self._register_as_active and get_active_runtime() is self:
                _run_cleanup_action(errors, lambda: set_active_runtime(None))

            lock_released = True
            try:
                self._lock.release()
            except Exception as exc:
                lock_released = False
                errors.append(exc)

            if lock_released:
                self._state = RuntimeState.CLOSED
                logger.info(f"AgentRuntime closed (instance_id={self.instance_id})")
        else:
            incomplete_parts: list[str] = []
            if not checkpointer_closed:
                incomplete_parts.append("checkpointer")
            if not mcp_disconnected:
                incomplete_parts.append("mcp")
            if not stores_closed:
                incomplete_parts.append("stores")
            logger.error(
                f"AgentRuntime shutdown incomplete (instance_id={self.instance_id}, "
                f"pending={incomplete_parts}); retaining SHUTTING_DOWN and RuntimeInstanceLock"
            )

        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup(
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
        hitl_manager: HITLManager | None = None,
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
            hitl_manager=hitl_manager,
            stores=stores,
            close_global_stores=close_global_stores,
            drain_timeout=drain_timeout,
        )
        await runtime.start()
        try:
            yield runtime
        finally:
            await runtime.shutdown()
