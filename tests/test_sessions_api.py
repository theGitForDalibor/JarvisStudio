"""Unit tests for Session management API endpoints."""

from fastapi.testclient import TestClient

from jarvis_studio.api.app import create_app
from jarvis_studio.service.session_service import get_session_service


def test_session_lifecycle_api():
    """Test full CRUD lifecycle for chat sessions via API."""
    app = create_app()
    with TestClient(app) as client:
        # 1. Create a session
        resp = client.post("/api/v1/sessions", json={"title": "Test Plan"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Test Plan"
        session_id = data["session_id"]
        assert session_id.startswith("sess_")

        # 2. List sessions
        list_resp = client.get("/api/v1/sessions")
        assert list_resp.status_code == 200
        sessions = list_resp.json()
        assert any(s["session_id"] == session_id for s in sessions)

        # 3. Add message via session service and get detail
        ss = get_session_service()
        ss.add_message(session_id, "user", "Hello world")
        ss.add_message(session_id, "assistant", "Hello there!")

        detail_resp = client.get(f"/api/v1/sessions/{session_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["session"]["session_id"] == session_id
        assert len(detail["messages"]) >= 2
        assert detail["messages"][0]["content"] == "Hello world"
        assert detail["messages"][1]["content"] == "Hello there!"

        # 4. Update session title
        patch_resp = client.patch(
            f"/api/v1/sessions/{session_id}", json={"title": "Renamed Session"}
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["title"] == "Renamed Session"

        # 5. Clear messages
        clear_resp = client.delete(f"/api/v1/sessions/{session_id}/messages")
        assert clear_resp.status_code == 200
        detail_after_clear = client.get(f"/api/v1/sessions/{session_id}").json()
        assert len(detail_after_clear["messages"]) == 0

        # 6. Delete session
        del_resp = client.delete(f"/api/v1/sessions/{session_id}")
        assert del_resp.status_code == 200
        del_check = client.get("/api/v1/sessions")
        assert not any(s["session_id"] == session_id for s in del_check.json())
