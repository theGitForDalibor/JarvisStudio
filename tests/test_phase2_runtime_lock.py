"""Phase 2 Stage 1 tests: OS-level RuntimeInstanceLock & Persistence Root guards."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.persistence import (
    RUNTIME_LOCK_FILENAME,
    RuntimeInstanceLock,
    RuntimeLockError,
    resolve_runtime_lock_paths,
)


def _make_settings(
    *,
    checkpoint_backend: str = "sqlite",
    sqlite_path: str | Path = ":memory:",
    vector_db_path: str | Path = ":memory:",
    rag_enabled: bool = True,
) -> Settings:
    return Settings(
        storage=StorageConfig(
            checkpoint_backend=checkpoint_backend,
            sqlite_path=str(sqlite_path),
            workspace_root="./data/test_workspace",
        ),
        rag=RAGConfig(
            enabled=rag_enabled,
            vector_db_path=str(vector_db_path),
        ),
    )


def _spawn_lock_worker(
    *,
    lock_dir: Path,
    sqlite_name: str = "jarvis_studio.db",
    vec_name: str = "jarvis_vec.db",
    backend: str = "sqlite",
    hold_seconds: float = 0.0,
    ready_file: Path | None = None,
) -> subprocess.Popen[str]:
    """Launch a real child Python process competing for the given persistence root lock."""
    script = """
import json
import sys
import time
from pathlib import Path

from jarvis_studio.core.persistence.runtime_lock import (
    RuntimeInstanceLock,
    RuntimeLockError,
    resolve_runtime_lock_paths,
)

lock_dir = Path(sys.argv[1])
sqlite_path = lock_dir / sys.argv[2]
vec_path = lock_dir / sys.argv[3]
backend = sys.argv[4]
hold_seconds = float(sys.argv[5])
ready_path = Path(sys.argv[6]) if sys.argv[6] else None

lock_paths = resolve_runtime_lock_paths(sqlite_path, vec_path)
lock = RuntimeInstanceLock(
    lock_paths,
    checkpoint_backend=backend,
    persistence_paths=(sqlite_path, vec_path),
)
try:
    info = lock.acquire()
except RuntimeLockError as exc:
    print(json.dumps({"status": "locked_error", "holder": exc.holder_metadata}))
    sys.exit(2)

if ready_path is not None:
    ready_path.write_text(str(info.pid), encoding="utf-8")

if hold_seconds > 0:
    time.sleep(hold_seconds)

lock.release()
print(json.dumps({"status": "acquired", "instance_id": info.instance_id, "pid": info.pid}))
"""
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else src_dir

    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(lock_dir),
            sqlite_name,
            vec_name,
            backend,
            str(hold_seconds),
            str(ready_file) if ready_file is not None else "",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_p2_lock_01_sqlite_backend_blocks_second_runtime_and_reports_holder(
    tmp_path: Path,
) -> None:
    """P2-LOCK-01: Second instance targeting the same persistence root fails closed."""
    settings = _make_settings(
        checkpoint_backend="sqlite",
        sqlite_path=tmp_path / "jarvis_studio.db",
        vector_db_path=tmp_path / "jarvis_vec.db",
    )

    lock1 = RuntimeInstanceLock.from_settings(settings, instance_id="inst-primary")
    lock2 = RuntimeInstanceLock.from_settings(settings, instance_id="inst-intruder")

    assert lock1.lock_paths == ((tmp_path / RUNTIME_LOCK_FILENAME).resolve(),)
    info1 = lock1.acquire()
    try:
        assert lock1.is_acquired is True
        assert info1.instance_id == "inst-primary"

        with pytest.raises(RuntimeLockError) as exc_info:
            lock2.acquire()

        assert exc_info.value.lock_path == (tmp_path / RUNTIME_LOCK_FILENAME).resolve()
        assert exc_info.value.holder_metadata is not None
        assert exc_info.value.holder_metadata["instance_id"] == "inst-primary"
        assert exc_info.value.holder_metadata["pid"] == os.getpid()
        assert exc_info.value.holder_metadata["checkpoint_backend"] == "sqlite"
        assert lock2.is_acquired is False
    finally:
        lock1.release()
        lock2.release()


def test_p2_lock_02_release_and_process_crash_allow_successor_without_unlinking(
    tmp_path: Path,
) -> None:
    """P2-LOCK-02: Releasing cleanly or process exit allows successor acquisition."""
    settings = _make_settings(
        checkpoint_backend="sqlite",
        sqlite_path=tmp_path / "jarvis_studio.db",
        vector_db_path=tmp_path / "jarvis_vec.db",
    )
    lock_file = (tmp_path / RUNTIME_LOCK_FILENAME).resolve()

    # 1. Clean release keeps the lock file inode intact (never unlinked) and allows re-entry
    lock1 = RuntimeInstanceLock.from_settings(settings, instance_id="inst-1")
    lock1.acquire()
    lock1.release()
    lock1.release()  # idempotent release
    assert lock_file.exists()

    lock2 = RuntimeInstanceLock.from_settings(settings, instance_id="inst-2")
    with lock2:
        assert lock2.is_acquired is True
    assert lock2.is_acquired is False

    # 2. Real child subprocess terminated mid-hold automatically yields lock via OS kernel
    ready_file = tmp_path / "worker.ready"
    with _spawn_lock_worker(
        lock_dir=tmp_path,
        backend="sqlite",
        hold_seconds=30.0,
        ready_file=ready_file,
    ) as proc:
        worker_pid: int | None = None
        try:
            for _ in range(100):
                if ready_file.exists():
                    raw_pid = ready_file.read_text(encoding="utf-8").strip()
                    if raw_pid:
                        worker_pid = int(raw_pid)
                        break
                assert proc.poll() is None, (
                    f"worker exited early: {proc.stderr.read() if proc.stderr else ''}"
                )
                time.sleep(0.05)
            assert worker_pid is not None

            contender = RuntimeInstanceLock.from_settings(settings, instance_id="inst-blocked")
            with pytest.raises(RuntimeLockError) as exc_info:
                contender.acquire()
            assert exc_info.value.holder_metadata is not None
            assert exc_info.value.holder_metadata["pid"] == worker_pid

            # Terminate actual worker PID (`.venv` on Windows uses a launcher shim)
            os.kill(worker_pid, signal.SIGTERM)
            proc.wait(timeout=5)

            successor = RuntimeInstanceLock.from_settings(settings, instance_id="inst-recovered")
            info = successor.acquire()
            try:
                assert info.instance_id == "inst-recovered"
            finally:
                successor.release()
        finally:
            if worker_pid is not None and proc.poll() is None:
                try:
                    os.kill(worker_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_p2_lock_03_concurrent_process_race_has_single_winner(tmp_path: Path) -> None:
    """P2-LOCK-03: Concurrent subprocesses competing for the same root elect strictly 1 winner."""
    worker_count = 4
    procs = [
        _spawn_lock_worker(lock_dir=tmp_path, backend="sqlite", hold_seconds=0.8)
        for _ in range(worker_count)
    ]
    results = []
    for proc in procs:
        with proc:
            stdout, stderr = proc.communicate(timeout=15)
            assert stderr == "", f"Unexpected worker stderr: {stderr}"
            results.append((proc.returncode, json.loads(stdout.strip())))

    winners = [item for code, item in results if code == 0]
    losers = [item for code, item in results if code == 2]
    assert len(winners) == 1
    assert len(losers) == worker_count - 1
    for loser in losers:
        assert loser["status"] == "locked_error"


def test_p2_lock_04_memory_checkpointer_still_locks_shared_disk_root_and_exempts_pure_memory(
    tmp_path: Path,
) -> None:
    """P2-LOCK-04 (R1): Memory checkpointer on disk DBs still locks; only all-:memory: skips lock."""
    # Case A: checkpoint_backend="memory", but disk SQLite paths are configured
    disk_memory_settings = _make_settings(
        checkpoint_backend="memory",
        sqlite_path=tmp_path / "jarvis_studio.db",
        vector_db_path=tmp_path / "jarvis_vec.db",
    )
    l1 = RuntimeInstanceLock.from_settings(disk_memory_settings, instance_id="mem-disk-1")
    l2 = RuntimeInstanceLock.from_settings(disk_memory_settings, instance_id="mem-disk-2")
    assert l1.is_noop is False

    l1.acquire()
    try:
        with pytest.raises(RuntimeLockError) as exc_info:
            l2.acquire()
        assert exc_info.value.holder_metadata is not None
        assert exc_info.value.holder_metadata["checkpoint_backend"] == "memory"
    finally:
        l1.release()

    # Case B: checkpoint_backend="memory", sqlite_path=":memory:", ONLY vector_db_path is on disk
    vec_only_settings = _make_settings(
        checkpoint_backend="memory",
        sqlite_path=":memory:",
        vector_db_path=tmp_path / "vec_only" / "jarvis_vec.db",
        rag_enabled=True,
    )
    vec_lock_1 = RuntimeInstanceLock.from_settings(vec_only_settings, instance_id="vec-1")
    vec_lock_2 = RuntimeInstanceLock.from_settings(vec_only_settings, instance_id="vec-2")
    assert vec_lock_1.is_noop is False
    vec_lock_1.acquire()
    try:
        with pytest.raises(RuntimeLockError):
            vec_lock_2.acquire()
    finally:
        vec_lock_1.release()

    # Case C: Fully ephemeral all-:memory: (or rag_enabled=False with sqlite=:memory:) -> exempt
    pure_memory_settings = _make_settings(
        checkpoint_backend="memory",
        sqlite_path=":memory:",
        vector_db_path=":memory:",
        rag_enabled=True,
    )
    rag_disabled_settings = _make_settings(
        checkpoint_backend="memory",
        sqlite_path=":memory:",
        vector_db_path=tmp_path / "unused_vec" / "jarvis_vec.db",
        rag_enabled=False,
    )
    for cfg in (pure_memory_settings, rag_disabled_settings):
        noop1 = RuntimeInstanceLock.from_settings(cfg, instance_id="noop-1")
        noop2 = RuntimeInstanceLock.from_settings(cfg, instance_id="noop-2")
        assert noop1.is_noop is True
        assert noop2.is_noop is True
        info1 = noop1.acquire()
        info2 = noop2.acquire()
        assert info1.lock_paths == ()
        assert info2.lock_paths == ()
        noop1.release()
        noop2.release()
        assert not (tmp_path / "unused_vec" / RUNTIME_LOCK_FILENAME).exists()


def test_split_directory_lock_ordering_and_partial_acquire_rollback(tmp_path: Path) -> None:
    """Split persistence directories lock in sorted order and roll back lock 1 if lock 2 fails."""
    dir_b = tmp_path / "b_dir"
    dir_a = tmp_path / "a_dir"
    paths = resolve_runtime_lock_paths(
        dir_b / "jarvis_studio.db",
        dir_a / "jarvis_vec.db",
    )
    assert paths == (
        (dir_a / RUNTIME_LOCK_FILENAME).resolve(),
        (dir_b / RUNTIME_LOCK_FILENAME).resolve(),
    )

    # Pre-lock dir_b only
    blocker_b = RuntimeInstanceLock([dir_b / RUNTIME_LOCK_FILENAME], instance_id="blocker-b")
    blocker_b.acquire()
    try:
        multi = RuntimeInstanceLock(paths, instance_id="multi")
        with pytest.raises(RuntimeLockError) as exc_info:
            multi.acquire()
        assert exc_info.value.lock_path == (dir_b / RUNTIME_LOCK_FILENAME).resolve()
        assert multi.is_acquired is False

        # Verify dir_a lock was rolled back cleanly and is immediately acquirable
        probe_a = RuntimeInstanceLock([dir_a / RUNTIME_LOCK_FILENAME], instance_id="probe-a")
        probe_a.acquire()
        probe_a.release()
    finally:
        blocker_b.release()


def test_reacquire_same_lock_instance_raises_runtime_error(tmp_path: Path) -> None:
    """Calling acquire() twice without release() fails fast instead of leaking descriptors."""
    lock = RuntimeInstanceLock(
        resolve_runtime_lock_paths(tmp_path / "app.db"),
        instance_id="double-acquire",
    )
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already acquired"):
            lock.acquire()
    finally:
        lock.release()
