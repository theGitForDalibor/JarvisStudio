"""Unit and integration tests for Long-Term Memory (LTM)."""

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from jarvis_studio.api.app import create_app
from jarvis_studio.cli.main import cli
from jarvis_studio.memory.manager import MemoryManager, get_memory_manager
from jarvis_studio.memory.models import MemoryCategory, MemoryItem
from jarvis_studio.memory.store import MemoryStore
from jarvis_studio.tools.builtin.memory_tools import (
    recall_personal_memory,
    save_personal_memory,
)


@pytest.fixture
def memory_store():
    store = MemoryStore(db_path=":memory:")
    yield store
    store.clear()


@pytest.fixture
def memory_manager(memory_store):
    mgr = MemoryManager(store=memory_store)
    # Set global manager for tools to use this isolated store
    get_memory_manager(store=memory_store)
    return mgr


def test_memory_store_crud(memory_store):
    """Verify basic CRUD on SQLite MemoryStore."""
    item = MemoryItem(
        id="mem1",
        category=MemoryCategory.PREFERENCE,
        content="Always use uv for package management",
        importance=0.9,
    )
    saved = memory_store.add(item)
    assert saved.id == "mem1"

    # Fetch
    fetched = memory_store.get("mem1")
    assert fetched is not None
    assert fetched.content == "Always use uv for package management"
    assert fetched.category == MemoryCategory.PREFERENCE

    # Update
    fetched.content = "Always use uv and Python 3.12"
    updated = memory_store.update(fetched)
    assert updated.content == "Always use uv and Python 3.12"

    # Search
    search_res = memory_store.search("Python")
    assert len(search_res) == 1
    assert search_res[0].id == "mem1"

    # Delete
    assert memory_store.delete("mem1") is True
    assert memory_store.get("mem1") is None


def test_memory_manager_context_formatting(memory_manager):
    """Verify formatted Markdown context generation for Agent prompt."""
    memory_manager.remember(
        content="Prefer pytest over unittest",
        category=MemoryCategory.PREFERENCE,
    )
    memory_manager.remember(
        content="Host is Windows 11 running PowerShell",
        category=MemoryCategory.PROFILE,
    )

    context = memory_manager.get_formatted_context()
    assert "[User Long-Term Memory & Preferences]" in context
    assert "PREFERENCE" in context
    assert "Prefer pytest over unittest" in context
    assert "PROFILE" in context


def test_memory_tools(memory_manager):
    """Verify save_personal_memory and recall_personal_memory tools."""
    # Tool 1: Save
    res_save = save_personal_memory.invoke(
        {
            "content": "Database is SQLite with sqlite-vec extension",
            "category": "project",
        }
    )
    assert "Successfully saved memory" in res_save

    # Tool 2: Recall
    res_recall = recall_personal_memory.invoke({"query": "sqlite-vec"})
    assert "Database is SQLite with sqlite-vec extension" in res_recall
    assert "project" in res_recall


def test_memory_api_endpoints():
    """Verify FastAPI memory routes."""
    app = create_app()
    with TestClient(app) as client:
        # Create
        create_resp = client.post(
            "/api/v1/memory",
            json={
                "content": "API test preference",
                "category": "preference",
                "importance": 0.8,
            },
        )
        assert create_resp.status_code == 200
        mem_data = create_resp.json()
        mem_id = mem_data["id"]

        # List
        list_resp = client.get("/api/v1/memory")
        assert list_resp.status_code == 200
        assert any(m["id"] == mem_id for m in list_resp.json())

        # Search
        search_resp = client.post(
            "/api/v1/memory/search",
            json={"query": "API test"},
        )
        assert search_resp.status_code == 200
        assert len(search_resp.json()) >= 1

        # Delete
        del_resp = client.delete(f"/api/v1/memory/{mem_id}")
        assert del_resp.status_code == 200


def test_cli_memory_commands():
    """Verify CLI commands: memory add, list, delete."""
    runner = CliRunner()

    add_res = runner.invoke(
        cli, ["memory", "add", "CLI test memory", "--category", "fact"]
    )
    assert add_res.exit_code == 0
    assert "Stored memory" in add_res.output

    list_res = runner.invoke(cli, ["memory", "list"])
    assert list_res.exit_code == 0
    assert "User Long-Term Memory & Preferences" in list_res.output
    assert "CLI test memory" in list_res.output
