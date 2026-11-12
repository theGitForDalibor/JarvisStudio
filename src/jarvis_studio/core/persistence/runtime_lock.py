"""Cross-platform OS-level single-writer instance lock for shared persistence roots.

Phase 2 Step 1:
- Guards shared file-backed persistence directories (`jarvis_studio.db`, `jarvis_vec.db`,
  and future Phase 3/4 runtime state) against concurrent active writers.
- Lock activation depends on whether persistent storage targets disk files, NOT on
  `checkpoint_backend == "sqlite"` (`memory` checkpointer with disk DBs still locks).
- Only all-`:memory:` ephemeral setups are exempt from acquiring an OS file lock.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jarvis_studio.core.persistence.sqlite import (
    is_memory_db_path,
    normalize_db_path,
)

if TYPE_CHECKING:
    from jarvis_studio.config.settings import Settings

if os.name == "nt":
    import msvcrt
else:
    import fcntl

RUNTIME_LOCK_FILENAME = ".jarvis_runtime.lock"
_LOCK_SENTINEL_PREFIX = b" "
_METADATA_READ_OFFSET = len(_LOCK_SENTINEL_PREFIX)
_MAX_METADATA_BYTES = 16 * 1024


class RuntimeLockError(RuntimeError):
    """Raised when another active runtime already holds a shared persistence root lock."""

    def __init__(
        self,
        message: str,
        *,
        lock_path: Path,
        holder_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.lock_path = lock_path
        self.holder_metadata = holder_metadata


@dataclass(frozen=True)
class RuntimeLockInfo:
    """Snapshot of an acquired or no-op runtime instance lock."""

    lock_paths: tuple[Path, ...]
    instance_id: str
    pid: int
    started_at: str
    checkpoint_backend: str | None
    metadata: dict[str, Any]


def resolve_runtime_lock_paths(
    sqlite_path: str | Path,
    vector_db_path: str | Path | None = None,
    *,
    rag_enabled: bool = True,
) -> tuple[Path, ...]:
    """Derive canonical `.jarvis_runtime.lock` paths covering all file-backed DBs.

    Returns an empty tuple only when every enabled persistence path is `:memory:`.
    When multiple file-backed DBs live in the same persistence root (default `data/`),
    they collapse to a single lock file; when spread across directories, directories
    are sorted deterministically by canonical string path to prevent lock-order deadlocks.
    """
    candidate_paths: list[str] = [normalize_db_path(sqlite_path)]
    if rag_enabled and vector_db_path is not None:
        candidate_paths.append(normalize_db_path(vector_db_path))

    root_dirs: set[Path] = set()
    for raw_path in candidate_paths:
        if is_memory_db_path(raw_path):
            continue
        root_dirs.add(Path(raw_path).resolve().parent)

    sorted_roots = sorted(root_dirs, key=lambda directory: str(directory))
    return tuple(directory / RUNTIME_LOCK_FILENAME for directory in sorted_roots)


def resolve_runtime_lock_paths_from_settings(
    settings: Settings | None = None,
) -> tuple[Path, ...]:
    """Derive canonical runtime lock paths from application settings."""
    if settings is None:
        from jarvis_studio.config.settings import get_settings

        settings = get_settings()

    return resolve_runtime_lock_paths(
        settings.storage.sqlite_path,
        settings.rag.vector_db_path,
        rag_enabled=settings.rag.enabled,
    )


def _try_lock_fd(fd: int) -> None:
    """Attempt non-blocking exclusive OS lock on an open file descriptor."""
    if os.name == "nt":
        # Lock sentinel byte [0, 1) while keeping JSON metadata readable from offset 1.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    """Release OS lock held on an open file descriptor."""
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_lock_metadata(fd: int, metadata: dict[str, Any]) -> None:
    """Overwrite lock file payload without ever shrinking below the locked sentinel byte."""
    encoded = _LOCK_SENTINEL_PREFIX + json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    written = 0
    while written < len(encoded):
        written += os.write(fd, encoded[written:])
    os.ftruncate(fd, len(encoded))


def _read_lock_metadata_fd(fd: int) -> dict[str, Any] | None:
    """Best-effort read of lock holder metadata without crossing byte 0 on Windows."""
    try:
        os.lseek(fd, _METADATA_READ_OFFSET, os.SEEK_SET)
        raw = os.read(fd, _MAX_METADATA_BYTES)
    except OSError:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _release_held_fds(held_fds: list[tuple[int, Path]]) -> None:
    """Unlock and close file descriptors in reverse acquisition order without unlinking."""
    errors: list[OSError] = []
    while held_fds:
        fd, _ = held_fds.pop()
        try:
            try:
                _unlock_fd(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


class RuntimeInstanceLock:
    """Single-writer OS file lock protecting one or more persistence root directories."""

    def __init__(
        self,
        lock_paths: Sequence[str | Path],
        *,
        instance_id: str | None = None,
        checkpoint_backend: str | None = None,
        persistence_paths: Sequence[str | Path] = (),
    ) -> None:
        unique_sorted = sorted(
            {Path(path).resolve() for path in lock_paths},
            key=lambda path: str(path),
        )
        self._lock_paths: tuple[Path, ...] = tuple(unique_sorted)
        self._instance_id: str = instance_id or uuid4().hex
        self._checkpoint_backend: str | None = checkpoint_backend
        self._persistence_paths: tuple[str, ...] = tuple(
            str(path) for path in persistence_paths
        )
        self._held_fds: list[tuple[int, Path]] = []
        self._acquired: bool = False
        self._info: RuntimeLockInfo | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        instance_id: str | None = None,
    ) -> RuntimeInstanceLock:
        """Build a RuntimeInstanceLock covering all enabled file-backed DBs in settings."""
        if settings is None:
            from jarvis_studio.config.settings import get_settings

            settings = get_settings()

        lock_paths = resolve_runtime_lock_paths_from_settings(settings)
        persistence_paths: list[str] = [settings.storage.sqlite_path]
        if settings.rag.enabled:
            persistence_paths.append(settings.rag.vector_db_path)

        return cls(
            lock_paths,
            instance_id=instance_id,
            checkpoint_backend=settings.storage.checkpoint_backend,
            persistence_paths=persistence_paths,
        )

    @property
    def is_noop(self) -> bool:
        """Return True when every configured DB is `:memory:` and no OS lock is needed."""
        return len(self._lock_paths) == 0

    @property
    def is_acquired(self) -> bool:
        """Return True while this lock instance holds active ownership."""
        return self._acquired

    @property
    def lock_paths(self) -> tuple[Path, ...]:
        """Canonical sorted `.jarvis_runtime.lock` paths managed by this instance."""
        return self._lock_paths

    @property
    def instance_id(self) -> str:
        """Unique identifier assigned to this runtime lock holder."""
        return self._instance_id

    @property
    def info(self) -> RuntimeLockInfo | None:
        """Return lock acquisition metadata if currently acquired."""
        return self._info

    def acquire(self) -> RuntimeLockInfo:
        """Acquire all persistence root locks fail-closed, rolling back on partial failure."""
        if self._acquired:
            raise RuntimeError(
                f"RuntimeInstanceLock ({self._instance_id}) is already acquired"
            )

        started_at = datetime.now(UTC).isoformat()
        pid = os.getpid()
        metadata: dict[str, Any] = {
            "pid": pid,
            "instance_id": self._instance_id,
            "started_at": started_at,
            "checkpoint_backend": self._checkpoint_backend,
            "persistence_paths": list(self._persistence_paths),
            "platform": sys.platform,
        }

        if self.is_noop:
            info = RuntimeLockInfo(
                lock_paths=(),
                instance_id=self._instance_id,
                pid=pid,
                started_at=started_at,
                checkpoint_backend=self._checkpoint_backend,
                metadata=metadata,
            )
            self._acquired = True
            self._info = info
            return info

        held_fds: list[tuple[int, Path]] = []
        try:
            for lock_path in self._lock_paths:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                locked = False
                try:
                    _try_lock_fd(fd)
                    locked = True
                except OSError as exc:
                    holder = _read_lock_metadata_fd(fd)
                    os.close(fd)
                    holder_desc = (
                        f" (held by pid={holder.get('pid')}, instance_id={holder.get('instance_id')})"
                        if holder
                        else ""
                    )
                    raise RuntimeLockError(
                        f"Persistence root is already locked by another active runtime: "
                        f"{lock_path}{holder_desc}",
                        lock_path=lock_path,
                        holder_metadata=holder,
                    ) from exc

                try:
                    _write_lock_metadata(fd, metadata)
                except BaseException:
                    try:
                        if locked:
                            _unlock_fd(fd)
                    finally:
                        os.close(fd)
                    raise

                held_fds.append((fd, lock_path))
        except BaseException:
            if held_fds:
                try:
                    _release_held_fds(held_fds)
                except OSError:
                    pass
            raise

        info = RuntimeLockInfo(
            lock_paths=self._lock_paths,
            instance_id=self._instance_id,
            pid=pid,
            started_at=started_at,
            checkpoint_backend=self._checkpoint_backend,
            metadata=metadata,
        )
        self._held_fds = held_fds
        self._acquired = True
        self._info = info
        return info

    def release(self) -> None:
        """Release held OS locks cleanly without unlinking lock files."""
        self._acquired = False
        self._info = None
        if not self._held_fds:
            return
        # Never unlink lock files on exit: removing an active lock path opens an inode race.
        _release_held_fds(self._held_fds)

    def __enter__(self) -> RuntimeLockInfo:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()
