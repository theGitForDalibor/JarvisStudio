"""Sprint 2A Integration Tests: Research Search & Report Publishing Tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.tasks.coordinator import get_task_coordinator, reset_global_task_coordinator
from jarvis_studio.service.deliverable_manager import get_deliverable_manager, reset_global_deliverable_manager
from jarvis_studio.service.task_service import get_task_service, reset_global_task_service
from jarvis_studio.tools.builtin.report_tools import publish_research_report
from jarvis_studio.tools.builtin.search_tools import (
    CompositeSearchEngine,
    DuckDuckGoSearchProvider,
    SearchResult,
    search_web,
)


@pytest.fixture
def research_env(tmp_path: Path):
    settings = get_settings()
    orig_db = getattr(settings.storage, "sqlite_path", None)
    orig_ws = settings.storage.workspace_root

    test_db = str(tmp_path / "research_test.db")
    test_ws = str(tmp_path / "research_ws")

    settings.storage.sqlite_path = test_db
    settings.storage.workspace_root = test_ws

    reset_global_task_coordinator()
    reset_global_deliverable_manager()
    reset_global_task_service()

    yield {
        "db": test_db,
        "ws": test_ws,
    }

    reset_global_task_coordinator()
    reset_global_deliverable_manager()
    reset_global_task_service()

    if orig_db is not None:
        settings.storage.sqlite_path = orig_db
    settings.storage.workspace_root = orig_ws


def test_search_web_with_verified_results(research_env):
    """search_web returns genuine structured citations."""
    mock_results = [
        SearchResult(
            title="SQLite-vec Documentation",
            url="https://alexgarcia.xyz/sqlite-vec/",
            snippet="A vector search SQLite extension written in C with zero dependencies.",
            source="duckduckgo",
        ),
        SearchResult(
            title="Chroma Technical Spec",
            url="https://docs.trychroma.com/",
            snippet="The AI-native open-source embedding database.",
            source="duckduckgo",
        ),
    ]

    with patch.object(DuckDuckGoSearchProvider, "search", return_value=mock_results):
        res = search_web.invoke({"query": "vector databases", "top_k": 2})
        assert "Search Results for 'vector databases'" in res
        assert "https://alexgarcia.xyz/sqlite-vec/" in res
        assert "https://docs.trychroma.com/" in res
        assert "A vector search SQLite extension" in res


def test_search_web_graceful_degradation_when_offline(research_env):
    """search_web does not fabricate citations when all search providers are exhausted."""
    with patch.object(CompositeSearchEngine, "search", return_value=([], "exhausted")):
        res = search_web.invoke({"query": "quantum computing zero day", "top_k": 3})
        assert "No verified search results found" in res
        assert "No synthetic or unverified citations were generated" in res


def test_publish_research_report_tool(research_env):
    """publish_research_report creates physical markdown deliverable and indexes metadata."""
    task_svc = get_task_service()
    deliv_mgr = get_deliverable_manager()

    task, run = task_svc.create_task("Vector DB Analysis", auto_start_run=True)

    report_markdown = (
        "# 2026 Embedded Vector Search Evaluation\n\n"
        "## Executive Summary\n"
        "This evaluation benchmarks sqlite-vec against other lightweight vector engines.\n\n"
        "## Citations\n"
        "- [1] https://alexgarcia.xyz/sqlite-vec/\n"
    )

    result_str = publish_research_report.invoke(
        {
            "task_id": task.task_id,
            "run_id": run.run_id,
            "title": "2026 Embedded Vector Search Evaluation",
            "markdown_content": report_markdown,
            "summary": "Evaluation of sqlite-vec performance and memory footprints.",
        }
    )

    assert "Successfully published research deliverable" in result_str
    assert "2026 Embedded Vector Search Evaluation" in result_str
    assert "SHA-256:" in result_str

    # Verify deliverable in DB
    deliverables = deliv_mgr.list_deliverables_for_task(task.task_id)
    assert len(deliverables) == 1
    d = deliverables[0]
    assert d.title == "2026 Embedded Vector Search Evaluation"
    assert d.run_id == run.run_id
    assert (Path(research_env["ws"]) / d.file_path).is_file()
    assert (Path(research_env["ws"]) / d.file_path).read_text(encoding="utf-8") == report_markdown.strip()


def test_publish_research_report_validations(research_env):
    """publish_research_report rejects missing parameters."""
    res1 = publish_research_report.invoke({"task_id": "", "title": "T", "markdown_content": "C"})
    assert "Error: task_id cannot be empty" in res1

    res2 = publish_research_report.invoke({"task_id": "t1", "title": "", "markdown_content": "C"})
    assert "Error: report title cannot be empty" in res2

    res3 = publish_research_report.invoke({"task_id": "t1", "title": "T", "markdown_content": ""})
    assert "Error: markdown_content cannot be empty" in res3
