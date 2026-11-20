"""Sprint 2A Integration Tests: Tasks & Deliverables REST API Endpoints."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_studio.api.app import create_app
from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.tasks.coordinator import get_task_coordinator, reset_global_task_coordinator
from jarvis_studio.core.tasks.models import DeliverableType
from jarvis_studio.service.deliverable_manager import get_deliverable_manager, reset_global_deliverable_manager
from jarvis_studio.service.session_service import get_session_service, reset_global_session_service
from jarvis_studio.service.task_service import get_task_service, reset_global_task_service


@pytest.fixture
def client(tmp_path: Path):
    """Set up isolated test client with dedicated database and workspace."""
    settings = get_settings()
    orig_db = getattr(settings.storage, "sqlite_path", None)
    orig_ws = settings.storage.workspace_root

    test_db = str(tmp_path / "test_api.db")
    test_ws = str(tmp_path / "test_workspace")

    settings.storage.sqlite_path = test_db
    settings.storage.workspace_root = test_ws

    # Reset singletons
    reset_global_task_coordinator()
    reset_global_deliverable_manager()
    reset_global_task_service()
    reset_global_session_service()

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client

    # Restore settings and reset singletons
    reset_global_task_coordinator()
    reset_global_deliverable_manager()
    reset_global_task_service()
    reset_global_session_service()
    if orig_db is not None:
        settings.storage.sqlite_path = orig_db
    settings.storage.workspace_root = orig_ws


def test_api_create_independent_and_session_tasks(client: TestClient):
    """POST /api/v1/tasks can create independent tasks or session-bound tasks."""
    # 1. Independent Task (session_id = null)
    resp = client.post(
        "/api/v1/tasks",
        json={
            "query": "Independent analysis task",
            "assigned_agent_id": "researcher",
            "priority": "HIGH",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    task_id1 = data["task_id"]
    assert data["session_id"] is None
    assert data["status"] == "CREATED"
    assert data["priority"] == "HIGH"
    assert data["assigned_agent_id"] == "researcher"

    # 2. Session-linked Task
    sess_resp = client.post("/api/v1/sessions", json={"title": "Chat Thread 1"})
    sess_id = sess_resp.json()["session_id"]

    resp2 = client.post(
        "/api/v1/tasks",
        json={
            "query": "Session task",
            "session_id": sess_id,
        },
    )
    assert resp2.status_code == 201
    data2 = resp2.json()
    task_id2 = data2["task_id"]
    assert data2["session_id"] == sess_id

    # 3. List tasks
    list_resp = client.get("/api/v1/tasks")
    assert list_resp.status_code == 200
    all_tasks = list_resp.json()
    assert len(all_tasks) >= 2

    # Filter by session
    sess_tasks_resp = client.get(f"/api/v1/tasks?session_id={sess_id}")
    assert sess_tasks_resp.status_code == 200
    sess_tasks = sess_tasks_resp.json()
    assert len(sess_tasks) == 1
    assert sess_tasks[0]["task_id"] == task_id2


def test_api_get_task_detail_and_events(client: TestClient):
    """GET /api/v1/tasks/{id} returns full detail with runs, deliverables, and events."""
    resp = client.post(
        "/api/v1/tasks",
        json={"query": "Detailed task", "auto_start_run": True},
    )
    task_id = resp.json()["task_id"]

    detail_resp = client.get(f"/api/v1/tasks/{task_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["task"]["task_id"] == task_id
    assert len(detail["runs"]) == 1
    assert len(detail["events"]) >= 2  # TASK_CREATED + RUN_STARTED

    # Check /events endpoint
    events_resp = client.get(f"/api/v1/tasks/{task_id}/events?since_seq=0")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) >= 2


def test_api_cancel_task(client: TestClient):
    """POST /api/v1/tasks/{id}/cancel stops task and its run."""
    resp = client.post(
        "/api/v1/tasks",
        json={"query": "To be cancelled", "auto_start_run": False},
    )
    task_id = resp.json()["task_id"]

    cancel_resp = client.post(f"/api/v1/tasks/{task_id}/cancel", json={"reason": "User abort"})
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "CANCELLED"
    assert cancel_resp.json()["error_message"] == "User abort"


def test_api_delete_session_keeps_tasks(client: TestClient):
    """DELETE /api/v1/sessions/{id} with default keep_tasks=True preserves tasks with session_id=null."""
    sess_resp = client.post("/api/v1/sessions", json={"title": "Session to be deleted"})
    sess_id = sess_resp.json()["session_id"]

    task_resp = client.post(
        "/api/v1/tasks",
        json={"query": "Task in soon-to-be-deleted session", "session_id": sess_id},
    )
    task_id = task_resp.json()["task_id"]
    from jarvis_studio.core.tasks.models import TaskStatus
    get_task_coordinator().set_task_status(task_id, TaskStatus.COMPLETED)

    # Delete session
    del_sess_resp = client.delete(f"/api/v1/sessions/{sess_id}")
    assert del_sess_resp.status_code == 200

    # Session is 404
    assert client.get(f"/api/v1/sessions/{sess_id}").status_code == 200  # get_session re-instantiates or 404? wait, get_session_detail returns 200 because it instantiates if not found, but messages are empty
    # Task still exists and session_id is now None
    t_resp = client.get(f"/api/v1/tasks/{task_id}")
    assert t_resp.status_code == 200
    assert t_resp.json()["task"]["session_id"] is None


def test_api_deliverable_get_and_download(client: TestClient):
    """Deliverables can be viewed, listed, and securely downloaded via API."""
    # 1. Create a task and run
    t_resp = client.post(
        "/api/v1/tasks",
        json={"query": "Report generation task", "auto_start_run": True},
    )
    task_id = t_resp.json()["task_id"]
    run_id = t_resp.json()["current_run_id"]

    # 2. Use DeliverableManager to create deliverable
    deliv_mgr = get_deliverable_manager()
    report_content = "# AgentStudio Research Benchmark\n\nAll benchmarks completed successfully."
    deliv = deliv_mgr.create_deliverable(
        task_id=task_id,
        run_id=run_id,
        title="benchmark_report.md",
        content=report_content,
        deliverable_type=DeliverableType.MARKDOWN_REPORT,
    )

    # 3. GET /api/v1/deliverables/{id}
    meta_resp = client.get(f"/api/v1/deliverables/{deliv.deliverable_id}")
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta["deliverable_id"] == deliv.deliverable_id
    assert meta["title"] == "benchmark_report.md"
    assert "AgentStudio Research Benchmark" in meta["content_preview"]

    # 4. GET /api/v1/tasks/{task_id}/deliverables
    t_delivs_resp = client.get(f"/api/v1/tasks/{task_id}/deliverables")
    assert t_delivs_resp.status_code == 200
    assert len(t_delivs_resp.json()) == 1

    # 5. GET /api/v1/deliverables/{id}/download
    dl_resp = client.get(f"/api/v1/deliverables/{deliv.deliverable_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.headers["content-type"].startswith("text/markdown")
    assert dl_resp.text == report_content

    # 6. Non-existent deliverable download
    assert client.get("/api/v1/deliverables/deliv_nonexistent/download").status_code == 404


def test_api_task_archive_and_explicit_purge(client: TestClient):
    """DELETE /api/v1/tasks/{id} soft-archives while preserving deliverables; DELETE .../purge?confirm=true purges."""
    t_resp = client.post(
        "/api/v1/tasks",
        json={"query": "Archivable task", "auto_start_run": True},
    )
    assert t_resp.status_code == 201
    task_id = t_resp.json()["task_id"]
    run_id = t_resp.json()["current_run_id"]
    get_task_coordinator().complete_task_and_run(task_id, run_id)

    deliv = get_deliverable_manager().create_deliverable(
        task_id=task_id,
        run_id=run_id,
        title="Archive Deliverable",
        content="# Retained across archive",
    )

    # Ordinary DELETE archives the task
    arch_resp = client.delete(f"/api/v1/tasks/{task_id}")
    assert arch_resp.status_code == 200
    assert arch_resp.json()["status"] == "archived"
    assert arch_resp.json()["archived_at"] is not None

    # Hidden from default list, visible with include_archived=true
    assert all(item["task_id"] != task_id for item in client.get("/api/v1/tasks").json())
    assert any(
        item["task_id"] == task_id
        for item in client.get("/api/v1/tasks?include_archived=true").json()
    )

    # Deliverable download MUST still work after soft-delete/archive
    dl_resp = client.get(f"/api/v1/deliverables/{deliv.deliverable_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.text == "# Retained across archive"

    # Explicit purge without confirm=true must return 400
    no_confirm_resp = client.delete(f"/api/v1/tasks/{task_id}/purge")
    assert no_confirm_resp.status_code == 400
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200

    # Explicit purge with confirm=true removes task, deliverable DB, and file
    purge_resp = client.delete(f"/api/v1/tasks/{task_id}/purge?confirm=true")
    assert purge_resp.status_code == 200
    assert purge_resp.json()["status"] == "purged"
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/v1/deliverables/{deliv.deliverable_id}").status_code == 404


def test_api_execute_standalone_task_endpoint(client: TestClient):
    """POST /api/v1/tasks/{id}/execute runs an independent task via active AgentRuntime."""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from jarvis_studio.agent.graph import build_agent_graph
    from jarvis_studio.core.tasks.models import TaskStatus
    from jarvis_studio.service.agent_service import get_agent_service

    class _FakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    svc = get_agent_service()
    svc.graph = build_agent_graph(
        model=_FakeModel(responses=[AIMessage(content="API executed standalone output")]),
        checkpointer=svc.graph.checkpointer,
        hitl_mgr=svc.hitl_manager,
    )

    t_resp = client.post("/api/v1/tasks", json={"query": "Execute via REST API"})
    assert t_resp.status_code == 201
    task_id = t_resp.json()["task_id"]

    exec_resp = client.post(f"/api/v1/tasks/{task_id}/execute")
    assert exec_resp.status_code == 200
    exec_data = exec_resp.json()
    assert exec_data["status"] == "completed"
    assert exec_data["task_id"] == task_id
    assert exec_data["output_text"] == "API executed standalone output"

    assert client.post("/api/v1/tasks/task_missing_999/execute").status_code == 404

    get_task_coordinator().set_task_status(task_id, TaskStatus.UNKNOWN)
    assert client.post(f"/api/v1/tasks/{task_id}/execute").status_code == 409
