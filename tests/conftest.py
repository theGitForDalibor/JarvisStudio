"""Pytest fixtures and configuration."""

import os
import shutil
from pathlib import Path

import pytest

# Pre-set test environment variables before tests run
os.environ["OPENAI_API_KEY"] = "sk-test-mock-key"
os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["WORKSPACE_ROOT"] = "./data/test_workspace"
os.environ["HITL_ENABLED"] = "true"


def _reset_runtime_singletons(*, close_stores: bool) -> None:
    """Reset cached settings and persistence singletons for test isolation."""
    import jarvis_studio.config.settings as settings_mod
    import jarvis_studio.core.observability.trace_logger as trace_logger_mod
    import jarvis_studio.memory.manager as memory_manager_mod
    import jarvis_studio.service.agent_service as agent_service_mod
    import jarvis_studio.service.session_service as session_service_mod

    if close_stores:
        if session_service_mod._global_session_service is not None:
            session_service_mod._global_session_service.close()
        if memory_manager_mod._global_memory_manager is not None:
            memory_manager_mod._global_memory_manager.store.close()
        if trace_logger_mod._global_trace_store is not None:
            trace_logger_mod._global_trace_store.close()

    session_service_mod._global_session_service = None
    memory_manager_mod._global_memory_manager = None
    trace_logger_mod._global_trace_store = None
    agent_service_mod._global_agent_service = None
    settings_mod._global_settings = None


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_databases(tmp_path_factory: pytest.TempPathFactory):
    """Redirect default app and vector SQLite paths to an isolated temp directory."""
    db_dir: Path = tmp_path_factory.mktemp("jarvis_test_dbs")
    prev_sqlite_path = os.environ.get("SQLITE_DB_PATH")
    prev_vec_path = os.environ.get("VECTOR_DB_PATH")

    os.environ["SQLITE_DB_PATH"] = str(db_dir / "jarvis_studio.db")
    os.environ["VECTOR_DB_PATH"] = str(db_dir / "jarvis_vec.db")
    _reset_runtime_singletons(close_stores=True)

    try:
        yield db_dir
    finally:
        _reset_runtime_singletons(close_stores=True)
        if prev_sqlite_path is None:
            os.environ.pop("SQLITE_DB_PATH", None)
        else:
            os.environ["SQLITE_DB_PATH"] = prev_sqlite_path
        if prev_vec_path is None:
            os.environ.pop("VECTOR_DB_PATH", None)
        else:
            os.environ["VECTOR_DB_PATH"] = prev_vec_path
        shutil.rmtree(db_dir, ignore_errors=True)
