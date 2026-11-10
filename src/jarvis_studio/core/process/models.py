"""Core models and exceptions for the async Process Execution Runtime."""

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from jarvis_studio.core.errors import JarvisError


class ProcessStatus(str, Enum):
    """Unified execution status outcomes for managed subprocesses."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"


class ProcessError(JarvisError):
    """Base exception for Process Execution Runtime failures."""


class ProcessIsolationError(ProcessError):
    """Raised when OS-level process tree isolation (e.g. Win32 Job Object) cannot be guaranteed."""


class ProcessLaunchError(ProcessError):
    """Raised when spawning or attaching pipes to a subprocess fails."""


class ProcessExecutionSpec(BaseModel):
    """Specification controlling how an isolated subprocess is launched and bounded."""

    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_output_bytes: int = Field(default=262_144, gt=0)
    drain_grace_seconds: float = Field(default=0.2, ge=0)
    graceful_termination_timeout: float = Field(default=2.0, gt=0)
    reap_timeout: float = Field(default=2.0, gt=0)
    cleanup_max_seconds: float = Field(default=5.0, gt=0)

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        if not value or not value[0].strip():
            raise ValueError(
                "ProcessExecutionSpec.argv must contain at least a valid program path."
            )
        for item in value:
            if "\x00" in item:
                raise ValueError(
                    "ProcessExecutionSpec.argv items cannot contain NUL bytes."
                )
        return value


class ProcessResult(BaseModel):
    """Unified structured result produced by the Process Execution Runtime."""

    status: ProcessStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    pid: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    stdout_bytes_total: int = 0
    stderr_bytes_total: int = 0
    stdout_bytes_retained: int = 0
    stderr_bytes_retained: int = 0
    error_message: str | None = None
