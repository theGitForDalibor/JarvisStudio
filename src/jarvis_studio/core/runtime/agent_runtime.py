"""Unified AgentRuntime lifecycle container (Phase 2 Stage 3).

Owns process-wide runtime infrastructure resources for one active host:
- Unique bound main Event Loop (`bound_loop`)
- `RuntimeInstanceLock` for shared persistence roots
- Managed checkpointer (`AsyncSqliteSaver` + `aiosqlite.Connection` or `MemorySaver`)
- `AgentService` bound strictly to `bound_loop` (without exposing `AgentService.close`)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

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


class AgentRuntime:
    """Single-loop host managing persistence lock, checkpointer, and `AgentService`."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        instance_id: str | None = None,
        isolate_incompatible_checkpoints: bool = True,
        strict_loop_binding: bool = True,
        register_as_active: bool = False,
    ) -> None:
        if settings is None:
            from jarvis_studio.config.settings import get_settings

            settings = get_settings()

        self._settings = settings
        self._lock = RuntimeInstanceLock.from_settings(settings, instance_id=instance_id)
        self._isolate_incompatible_checkpoints = isolate_incompatible_checkpoints
        self._strict_loop_binding = strict_loop_binding
        self._register_as_active = register_as_active

        self._state: RuntimeState = RuntimeState.CREATED
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._managed_sqlite: ManagedSqliteCheckpointer | None = None
        self._checkpointer: BaseCheckpointSaver | None = None
        self._agent_service: AgentService | None = None

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
    def agent_service(self) -> AgentService:
        """Bound `AgentService` orchestrated by this runtime."""
        if self._agent_service is None or self._state != RuntimeState.READY:
            raise RuntimeError(
                f"AgentRuntime is not ready (state={self._state.value}); cannot access AgentService"
            )
        return self._agent_service

    async def start(self) -> AgentRuntime:
        """Bind main Event Loop, acquire persistence lock, open checkpointer, and start service."""
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

            from jarvis_studio.service.agent_service import AgentService

            self._checkpointer = saver
            self._agent_service = AgentService(
                checkpointer=saver,
                bound_loop=loop,
                strict_loop_binding=self._strict_loop_binding,
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

    async def shutdown(self) -> None:
        """Idempotently close managed resources and release persistence lock."""
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

        if self._register_as_active and get_active_runtime() is self:
            _run_cleanup_action(errors, lambda: set_active_runtime(None))

        if self._managed_sqlite is not None:
            managed = self._managed_sqlite
            self._managed_sqlite = None
            await _run_async_cleanup_action(errors, managed.aclose)

        self._agent_service = None
        self._checkpointer = None

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
    ) -> AsyncIterator[AgentRuntime]:
        """Async context manager that starts an `AgentRuntime` and always shuts it down."""
        runtime = cls(
            settings,
            instance_id=instance_id,
            isolate_incompatible_checkpoints=isolate_incompatible_checkpoints,
            strict_loop_binding=strict_loop_binding,
            register_as_active=register_as_active,
        )
        await runtime.start()
        try:
            yield runtime
        finally:
            await runtime.shutdown()
