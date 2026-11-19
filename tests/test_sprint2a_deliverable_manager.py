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
