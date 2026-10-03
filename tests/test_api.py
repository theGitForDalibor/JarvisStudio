"""Integration tests for FastAPI application endpoints."""

import pytest
from fastapi.testclient import TestClient
from jarvis_studio.api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_root_endpoint(client):
    """Verify root endpoint status."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Jarvis Studio"
    assert data["status"] == "online"


def test_system_health(client):
    """Verify health probe."""
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_system_status(client):
    """Verify system status details."""
    response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["hitl_enabled"] is True
    assert data["registered_tools_count"] > 0


def test_list_tools(client):
    """Verify tools inspection endpoint."""
    response = client.get("/api/v1/system/tools")
    assert response.status_code == 200
    tools = response.json()
    tool_names = [t["name"] for t in tools]
    assert "read_file" in tool_names
    assert "execute_shell_command" in tool_names
