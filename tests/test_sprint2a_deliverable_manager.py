"""Sprint 2A Integration Tests: DeliverableManager, Versioning, Storage, and Path Security."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import DeliverableType
from jarvis_studio.service.deliverable_manager import (
    MAX_DELIVERABLE_SIZE_BYTES,
    DeliverableManager,
)


@pytest.fixture
def deliv_fixture(tmp_path: Path):
    db_path = tmp_path / "deliv_test.db"
    ws_dir = tmp_path / "test_workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    coordinator = TaskCoordinator(db_path=db_path)
    mgr = DeliverableManager(db_path=db_path, workspace_root=ws_dir)

    # Create dummy task and run
    task = coordinator.create_task(query="Research Test Task")
    run1 = coordinator.start_run(task.task_id, runtime_id="rt_1")

    yield {
        "db_path": db_path,
        "ws_dir": ws_dir,
        "coordinator": coordinator,
        "mgr": mgr,
        "task": task,
        "run1": run1,
    }

    coordinator.close()
    mgr.close()


def test_deliverable_creation_and_metadata(deliv_fixture):
    """Deliverable creation produces atomic physical files, SHA-256 fingerprint, and summary."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    content = (
        "# 2026 Edge Vector Databases Overview\n\n"
        "This report evaluates embedded vector engines including sqlite-vec and Chromadb.\n\n"
        "sqlite-vec provides zero-dependency C vector search embedded in SQLite."
    )

    deliv = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="Edge Vector Databases Report",
        content=content,
        deliverable_type=DeliverableType.MARKDOWN_REPORT,
    )

    assert deliv.deliverable_id.startswith("deliv_")
    assert deliv.task_id == task.task_id
    assert deliv.run_id == run1.run_id
    assert deliv.file_path == f"deliverables/{task.task_id}/run_1/Edge_Vector_Databases_Report.md"
    assert deliv.file_size_bytes == len(content.encode("utf-8"))
    assert deliv.sha256_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert "evaluates embedded vector engines" in deliv.summary
    assert content.startswith(deliv.content_preview[:30])

    # Verify physical file existence
    file_path = mgr.get_deliverable_file_path(deliv.deliverable_id)
    assert file_path.is_file()
    assert file_path.read_text(encoding="utf-8") == content


def test_multi_run_versioning_and_file_isolation(deliv_fixture):
    """Successive runs store deliverables in separate subdirectories without overwriting prior runs."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    coordinator: TaskCoordinator = deliv_fixture["coordinator"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    # Run 1 deliverable (Draft)
    deliv1 = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="benchmark_report.md",
        content="Draft version for Run 1",
    )

    # Start Run 2
    run2 = coordinator.start_run(task.task_id, runtime_id="rt_1")
    assert run2.run_seq == 2

    # Run 2 deliverable (Final) with the same filename
    deliv2 = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run2.run_id,
        title="benchmark_report.md",
        content="Final version for Run 2 with benchmarks",
    )

    # Assert paths are isolated
    assert deliv1.file_path == f"deliverables/{task.task_id}/run_1/benchmark_report.md"
    assert deliv2.file_path == f"deliverables/{task.task_id}/run_2/benchmark_report.md"

    # Both files must coexist on disk with distinct content
    f1 = mgr.get_deliverable_file_path(deliv1.deliverable_id)
    f2 = mgr.get_deliverable_file_path(deliv2.deliverable_id)
    assert f1 != f2
    assert f1.read_text(encoding="utf-8") == "Draft version for Run 1"
    assert f2.read_text(encoding="utf-8") == "Final version for Run 2 with benchmarks"


def test_deliverable_size_limit_rejection(deliv_fixture):
    """Deliverable content exceeding 20MB is rejected."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]

    huge_content = b"X" * (MAX_DELIVERABLE_SIZE_BYTES + 1024)

    with pytest.raises(ValueError, match="exceeds maximum limit"):
        mgr.create_deliverable(
            task_id=task.task_id,
            run_id=run1.run_id,
            title="Oversized File",
            content=huge_content,
        )


def test_deliverable_empty_content_rejection(deliv_fixture):
    """Empty deliverable content is rejected."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]

    with pytest.raises(ValueError, match="cannot be empty"):
        mgr.create_deliverable(
            task_id=task.task_id,
            run_id=run1.run_id,
            title="Empty File",
            content="",
        )


def test_path_traversal_defense_on_file_access(deliv_fixture):
    """Attempts to escape the deliverables root via malicious file_path are blocked."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    # Manually insert a malicious path into DB to simulate tampering
    malicious_deliv_id = "deliv_tampered"
    with mgr._tx() as conn:
        conn.execute(
            """
            INSERT INTO agent_deliverables (
                deliverable_id, task_id, run_id, title, deliverable_type,
                file_path, mime_type, file_size_bytes, sha256_hash,
                summary, content_preview, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                malicious_deliv_id,
                task.task_id,
                run1.run_id,
                "Tampered Report",
                "file",
                "../../sensitive_passwords.txt",
                "text/plain",
                100,
                "dummyhash",
                "summary",
                "preview",
                None,
                1.0,
                1.0,
            ),
        )

    # Accessing this deliverable file path must raise PermissionError
    with pytest.raises(PermissionError, match="escapes"):
        mgr.get_deliverable_file_path(malicious_deliv_id)


def test_delete_task_deliverables_directory(deliv_fixture):
    """delete_task_deliverables_dir recursively purges task files and leaves others unharmed."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    # Deliverable for task
    deliv = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="report.md",
        content="Content to be deleted",
    )
    task_dir = ws_dir / "deliverables" / task.task_id
    assert task_dir.is_dir()

    # Another task deliverable
    other_dir = ws_dir / "deliverables" / "task_other_preserve"
    other_dir.mkdir(parents=True, exist_ok=True)
    other_file = other_dir / "preserve.txt"
    other_file.write_text("Don't touch me", encoding="utf-8")

    # Purge
    mgr.delete_task_deliverables_dir(task.task_id)
    assert not task_dir.exists()
    assert other_dir.exists()
    assert other_file.is_file()


def test_file_write_succeeds_db_commit_fails_no_orphan_and_no_clobber(
    deliv_fixture, monkeypatch: pytest.MonkeyPatch
):
    """If DB insert fails, only the failed call's file is rolled back; existing same-run deliverable stays intact."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    # 1. Existing committed deliverable with report.md
    first = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="report.md",
        content="Committed version",
    )
    first_path = mgr.get_deliverable_file_path(first.deliverable_id)
    assert first_path.read_text(encoding="utf-8") == "Committed version"

    # 2. Inject DB transaction failure on the next create_deliverable
    from contextlib import contextmanager

    @contextmanager
    def _failing_tx():
        raise RuntimeError("Simulated DB commit failure")
        yield  # pragma: no cover

    monkeypatch.setattr(mgr, "_tx", _failing_tx)

    with pytest.raises(RuntimeError, match="Simulated DB commit failure"):
        mgr.create_deliverable(
            task_id=task.task_id,
            run_id=run1.run_id,
            title="report.md",
            content="Failed second version",
        )

    # Restore _tx for verification
    monkeypatch.undo()
    # Pre-existing deliverable MUST remain completely intact on disk and DB!
    assert first_path.is_file()
    assert first_path.read_text(encoding="utf-8") == "Committed version"
    deliverables = mgr.list_deliverables_for_task(task.task_id)
    assert len(deliverables) == 1
    assert deliverables[0].deliverable_id == first.deliverable_id
    # No orphan or leftover temp file
    report = mgr.reconcile_storage(task_id=task.task_id, cleanup_temp=False)
    assert report.orphan_files == ()
    assert report.stale_temp_files == ()
    assert report.healthy_records == 1


def test_file_write_fails_leaves_no_db_record_or_temp_file(
    deliv_fixture, monkeypatch: pytest.MonkeyPatch
):
    """If physical write/replace fails, .tmp file is cleaned up and no DB row is created."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    import jarvis_studio.service.deliverable_manager as deliv_mod

    def _fail_replace(src, dst):
        raise OSError("Simulated disk full during atomic replace")

    monkeypatch.setattr(deliv_mod.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="Simulated disk full"):
        mgr.create_deliverable(
            task_id=task.task_id,
            run_id=run1.run_id,
            title="Failed Write Report",
            content="Should not persist",
        )

    assert mgr.list_deliverables_for_task(task.task_id) == []
    run_dir = ws_dir / "deliverables" / task.task_id / "run_1"
    if run_dir.exists():
        assert list(run_dir.iterdir()) == []


def test_reconcile_detects_and_cleans_crash_temp_and_orphan_files(deliv_fixture):
    """reconcile_storage detects & cleans .tmp/.purging crash artifacts and inspects/cleans orphan files."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    ws_dir: Path = deliv_fixture["ws_dir"]

    committed = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="valid.md",
        content="# Valid Deliverable",
    )

    run_dir = ws_dir / "deliverables" / task.task_id / "run_1"
    stale_tmp = run_dir / ".tmp_crashed_1234.tmp"
    stale_tmp.write_text("partial", encoding="utf-8")

    purging_dir = mgr.deliverables_dir / f".purging_{task.task_id}_abcd1234"
    purging_dir.mkdir(parents=True, exist_ok=True)
    (purging_dir / "old.txt").write_text("old", encoding="utf-8")

    orphan_file = run_dir / "orphan_unindexed.txt"
    orphan_file.write_text("orphan", encoding="utf-8")

    # 1. Inspect with cleanup_temp=True, cleanup_orphans=False (safe default)
    report1 = mgr.reconcile_storage(task_id=task.task_id, cleanup_temp=True, cleanup_orphans=False)
    assert report1.checked_records == 1
    assert report1.healthy_records == 1
    assert len(report1.stale_temp_files) == 2
    assert len(report1.cleaned_temp_files) == 2
    assert not stale_tmp.exists()
    assert not purging_dir.exists()
    assert len(report1.orphan_files) == 1
    assert report1.cleaned_orphan_files == ()
    assert orphan_file.is_file()  # safe default does not delete unknown files

    # 2. Explicit orphan cleanup
    report2 = mgr.reconcile_storage(task_id=task.task_id, cleanup_temp=True, cleanup_orphans=True)
    assert len(report2.cleaned_orphan_files) == 1
    assert not orphan_file.exists()
    assert mgr.get_deliverable_file_path(committed.deliverable_id).is_file()


def test_verify_and_reconcile_detect_missing_and_corrupted_deliverables(deliv_fixture):
    """verify_deliverable and reconcile_storage catch missing disk files and SHA-256 tampering."""
    mgr: DeliverableManager = deliv_fixture["mgr"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]

    d_missing = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="vanishing.md",
        content="Will be deleted externally",
    )
    d_corrupt = mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run1.run_id,
        title="tampered.md",
        content="Original untampered content",
    )

    mgr.get_deliverable_file_path(d_missing.deliverable_id).unlink()
    mgr.get_deliverable_file_path(d_corrupt.deliverable_id).write_text("Tampered!", encoding="utf-8")

    assert mgr.verify_deliverable(d_missing.deliverable_id) == (False, "file_missing")
    assert mgr.verify_deliverable(d_corrupt.deliverable_id) == (False, "hash_mismatch")
    with pytest.raises(FileNotFoundError):
        mgr.get_deliverable_file_path(d_missing.deliverable_id)

    report = mgr.reconcile_storage(task_id=task.task_id)
    assert report.checked_records == 2
    assert report.healthy_records == 0
    assert report.missing_files == (d_missing.deliverable_id,)
    assert report.corrupted_hashes == (d_corrupt.deliverable_id,)


def test_concurrent_same_run_and_multi_run_writes_never_clobber(deliv_fixture):
    """Concurrent writes with identical filenames across and within runs produce distinct intact deliverables."""
    from concurrent.futures import ThreadPoolExecutor

    mgr: DeliverableManager = deliv_fixture["mgr"]
    coordinator: TaskCoordinator = deliv_fixture["coordinator"]
    task = deliv_fixture["task"]
    run1 = deliv_fixture["run1"]
    run2 = coordinator.start_run(task.task_id, runtime_id="rt_1")

    # Cross-task run_id mismatch must be rejected
    other_task = coordinator.create_task(query="Other task")
    other_run = coordinator.start_run(other_task.task_id, runtime_id="rt_1")
    with pytest.raises(ValueError, match="does not belong to task"):
        mgr.create_deliverable(
            task_id=task.task_id,
            run_id=other_run.run_id,
            title="illegal.md",
            content="wrong run",
        )

    specs = [
        (run1.run_id, f"Run1 Content #{i}") for i in range(4)
    ] + [
        (run2.run_id, f"Run2 Content #{i}") for i in range(4)
    ]

    def _worker(item: tuple[str, str]):
        rid, body = item
        return mgr.create_deliverable(
            task_id=task.task_id,
            run_id=rid,
            title="report.md",
            content=body,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(_worker, specs))

    assert len(created) == 8
    paths = [d.file_path for d in created]
    assert len(set(paths)) == 8  # Zero filename collisions or silent overwrites

    for d, (_, expected_body) in zip(created, specs):
        ok, reason = mgr.verify_deliverable(d.deliverable_id)
        assert ok is True, reason
        assert mgr.read_deliverable_content(d.deliverable_id).decode("utf-8") == expected_body

    report = mgr.reconcile_storage(task_id=task.task_id, cleanup_temp=False)
    assert report.checked_records == 8
    assert report.healthy_records == 8
    assert report.orphan_files == ()
    assert report.stale_temp_files == ()
