"""Pytest fixtures and configuration."""

import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# Pre-set test environment variables before tests run
os.environ["OPENAI_API_KEY"] = "sk-test-mock-key"
os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["WORKSPACE_ROOT"] = "./data/test_workspace"
os.environ["HITL_ENABLED"] = "true"


def _pop_module_attr(module_name: str, attr_name: str) -> object | None:
    """Return and clear a cached module-level singleton if the module is loaded."""
    module = sys.modules.get(module_name)
    if module is None:
        return None
    value = getattr(module, attr_name, None)
    setattr(module, attr_name, None)
    return value


def _run_teardown_step(errors: list[BaseException], action: Callable[[], object]) -> None:
    """Run one teardown step and record any failure without skipping remaining cleanup."""
    try:
        action()
    except BaseException as exc:
        errors.append(exc)


def _raise_collected_errors(message: str, errors: list[BaseException]) -> None:
    """Re-raise a single cleanup error or preserve multiple failures together."""
    if len(errors) == 1:
        raise errors[0]
    if len(errors) > 1:
        raise BaseExceptionGroup(message, errors)


def _restore_env_var(name: str, previous: str | None) -> None:
    """Restore an environment variable to its previous value or unset it."""
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def _reset_runtime_singletons(*, close_stores: bool) -> None:
    """Reset cached settings and persistence singletons in dependency order."""
    errors: list[BaseException] = []

    # 1. Detach top-level orchestrator first (AgentService exposes no close();
    # its compiled graph uses in-memory MemorySaver and refs SessionService/HITLManager).
    agent_service = _pop_module_attr("jarvis_studio.service.agent_service", "_global_agent_service")
    default_agent_service = _pop_module_attr(
        "jarvis_studio.service.agent_service", "_default_agent_service"
    )
    session_service = _pop_module_attr(
        "jarvis_studio.service.session_service", "_global_session_service"
    )
    memory_manager = _pop_module_attr("jarvis_studio.memory.manager", "_global_memory_manager")
    trace_store = _pop_module_attr(
        "jarvis_studio.core.observability.trace_logger", "_global_trace_store"
    )
    hitl_manager = _pop_module_attr("jarvis_studio.agent.hitl.manager", "_global_hitl_manager")
    default_hitl_manager = _pop_module_attr(
        "jarvis_studio.agent.hitl.manager", "_default_hitl_manager"
    )
    mcp_manager = _pop_module_attr("jarvis_studio.tools.mcp.client", "_global_mcp_manager")
    registry = _pop_module_attr("jarvis_studio.tools.registry", "_global_registry")
    _pop_module_attr("jarvis_studio.agent.checkpoints.saver", "_cached_checkpointer")
    _pop_module_attr("jarvis_studio.core.guard.route_guard", "_global_guard")
    _pop_module_attr("jarvis_studio.core.runtime.agent_runtime", "_active_runtime")

    try:
        if close_stores:
            seen_close_ids: set[int] = set()
            for candidate in (
                getattr(agent_service, "session_service", None),
                getattr(default_agent_service, "session_service", None),
                session_service,
                getattr(memory_manager, "store", None),
                trace_store,
            ):
                close_fn = getattr(candidate, "close", None)
                if callable(close_fn) and id(candidate) not in seen_close_ids:
                    seen_close_ids.add(id(candidate))
                    _run_teardown_step(errors, close_fn)

            seen_clear_ids: set[int] = set()
            for candidate in (
                hitl_manager,
                default_hitl_manager,
                getattr(agent_service, "hitl_manager", None),
                getattr(default_agent_service, "hitl_manager", None),
            ):
                clear_fn = getattr(candidate, "clear", None)
                if callable(clear_fn) and id(candidate) not in seen_clear_ids:
                    seen_clear_ids.add(id(candidate))
                    _run_teardown_step(errors, clear_fn)

            seen_hitl_close_ids: set[int] = set()
            for candidate in (
                hitl_manager,
                default_hitl_manager,
                getattr(agent_service, "hitl_manager", None),
                getattr(default_agent_service, "hitl_manager", None),
            ):
                close_fn = getattr(candidate, "close", None)
                if callable(close_fn) and id(candidate) not in seen_hitl_close_ids:
                    seen_hitl_close_ids.add(id(candidate))
                    _run_teardown_step(errors, close_fn)

            connections = getattr(mcp_manager, "connections", None)
            if isinstance(connections, dict) and any(
                getattr(conn, "is_connected", False)
                or getattr(conn, "_exit_stack", None) is not None
                for conn in connections.values()
            ):
                _run_teardown_step(
                    errors,
                    lambda mgr=mcp_manager, reg=registry: asyncio.run(
                        mgr.disconnect_all(registry=reg)  # type: ignore[union-attr]
                    ),
                )
    finally:
        _pop_module_attr("jarvis_studio.config.settings", "_global_settings")

    _raise_collected_errors(
        "Multiple errors occurred while resetting runtime singletons", errors
    )


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_databases(tmp_path_factory: pytest.TempPathFactory):
    """Redirect default app and vector SQLite paths to an isolated temp directory."""
    db_dir: Path = tmp_path_factory.mktemp("jarvis_test_dbs")
    prev_sqlite_path = os.environ.get("SQLITE_DB_PATH")
    prev_vec_path = os.environ.get("VECTOR_DB_PATH")

    os.environ["SQLITE_DB_PATH"] = str(db_dir / "jarvis_studio.db")
    os.environ["VECTOR_DB_PATH"] = str(db_dir / "jarvis_vec.db")

    try:
        _reset_runtime_singletons(close_stores=True)
    except BaseException as setup_exc:
        setup_cleanup_errors: list[BaseException] = []
        for env_name, prev_val in (
            ("SQLITE_DB_PATH", prev_sqlite_path),
            ("VECTOR_DB_PATH", prev_vec_path),
        ):
            _run_teardown_step(
                setup_cleanup_errors,
                lambda name=env_name, val=prev_val: _restore_env_var(name, val),
            )
        _run_teardown_step(
            setup_cleanup_errors,
            lambda: shutil.rmtree(db_dir) if db_dir.exists() else None,
        )
        if setup_cleanup_errors:
            setup_exc.add_note(
                "Cleanup error(s) after setup failure: "
                + "; ".join(f"{type(e).__name__}: {e}" for e in setup_cleanup_errors)
            )
        raise

    try:
        yield db_dir
    finally:
        teardown_errors: list[BaseException] = []
        _run_teardown_step(teardown_errors, lambda: _reset_runtime_singletons(close_stores=True))
        for env_name, prev_val in (
            ("SQLITE_DB_PATH", prev_sqlite_path),
            ("VECTOR_DB_PATH", prev_vec_path),
        ):
            _run_teardown_step(
                teardown_errors,
                lambda name=env_name, val=prev_val: _restore_env_var(name, val),
            )
        _run_teardown_step(
            teardown_errors,
            lambda: shutil.rmtree(db_dir) if db_dir.exists() else None,
        )

        _raise_collected_errors(
            "Multiple errors occurred during test database isolation teardown",
            teardown_errors,
        )
