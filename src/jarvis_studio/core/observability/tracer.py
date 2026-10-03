"""Tracing and metrics collection context for Jarvis Studio."""

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Generator, Optional
from pydantic import BaseModel, Field

from jarvis_studio.core.observability.logger import logger

_RUN_ID_VAR: ContextVar[Optional[str]] = ContextVar("current_run_id", default=None)
_SESSION_ID_VAR: ContextVar[Optional[str]] = ContextVar("current_session_id", default=None)


class StepMetrics(BaseModel):
    """Metrics for an individual execution step or tool call."""
    step_name: str
    duration_ms: float
    status: str = "success"
    metadata: Dict[str, Any] = Field(default_factory=dict)


def get_current_run_id() -> str:
    """Return the active Run ID or generate a new one."""
    run_id = _RUN_ID_VAR.get()
    if not run_id:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        _RUN_ID_VAR.set(run_id)
    return run_id


def set_trace_context(session_id: str, run_id: Optional[str] = None) -> str:
    """Set the session and run context for the active coroutine."""
    actual_run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
    _SESSION_ID_VAR.set(session_id)
    _RUN_ID_VAR.set(actual_run_id)
    return actual_run_id


def get_current_session_id() -> Optional[str]:
    """Return the active Session ID."""
    return _SESSION_ID_VAR.get()


@contextmanager
def timed_step(step_name: str, **extra_meta: Any) -> Generator[StepMetrics, None, None]:
    """Context manager to measure latency of an operation with structured logging."""
    run_id = get_current_run_id()
    session_id = get_current_session_id() or "unknown"
    start_time = time.perf_counter()
    metrics = StepMetrics(step_name=step_name, duration_ms=0.0, metadata=extra_meta)
    
    logger.debug(
        f"[{session_id}][{run_id}] START step '{step_name}' | Meta: {extra_meta}"
    )
    try:
        yield metrics
        metrics.status = "success"
    except Exception as exc:
        metrics.status = "error"
        metrics.metadata["error"] = str(exc)
        logger.error(
            f"[{session_id}][{run_id}] FAILED step '{step_name}': {exc}"
        )
        raise
    finally:
        duration = (time.perf_counter() - start_time) * 1000.0
        metrics.duration_ms = round(duration, 2)
        logger.info(
            f"[{session_id}][{run_id}] END step '{step_name}' in {metrics.duration_ms}ms [{metrics.status}]"
        )
