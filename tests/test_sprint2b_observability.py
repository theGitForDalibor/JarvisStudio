"""Tests for Sprint 2B Observability: EventBroadcaster, Tool Events, and Replayable SSE Streaming."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from jarvis_studio.agent.nodes.executor import _sanitize_tool_args, create_tool_executor_node
from jarvis_studio.agent.state import AgentState
from jarvis_studio.api.routes.tasks import router as tasks_router
from jarvis_studio.config.models import StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.observability.event_broadcaster import EventBroadcaster, get_event_broadcaster
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import EventRecord, EventType, TaskStatus
from jarvis_studio.service.task_service import TaskService
from jarvis_studio.tools.registry import ToolRegistry


def _make_settings(tmp_path: Path, db_name: str) -> Settings:
    return Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(tmp_path / db_name),
            workspace_root=str(tmp_path / "workspace"),
        )
    )


@pytest.mark.asyncio
async def test_event_broadcaster_pubsub() -> None:
    """Verify EventBroadcaster delivers events to active task subscribers and handles cleanup."""
    broadcaster = EventBroadcaster()
    task_id = "task_test_pubsub"

    event1 = EventRecord(
        event_id="e1",
        task_id=task_id,
        run_id="r1",
        seq=1,
        event_type=EventType.RUN_STARTED,
        payload_json='{"status":"started"}',
    )
    event2 = EventRecord(
        event_id="e2",
        task_id=task_id,
        run_id="r1",
        seq=2,
        event_type=EventType.TASK_COMPLETED,
        payload_json='{"status":"done"}',
    )
    other_event = EventRecord(
        event_id="e3",
        task_id="other_task",
        run_id="r2",
        seq=1,
        event_type=EventType.RUN_STARTED,
        payload_json="{}",
    )

    async with broadcaster.subscribe(task_id) as q1, broadcaster.subscribe(task_id) as q2:
        assert await broadcaster.get_subscriber_count(task_id) == 2

        broadcaster.broadcast(event1)
        broadcaster.broadcast(other_event)
        broadcaster.broadcast(event2)

        # Both subscribers should receive event1 and event2, but NOT other_event
        rec1_a = await asyncio.wait_for(q1.get(), timeout=1.0)
        rec1_b = await asyncio.wait_for(q1.get(), timeout=1.0)
        assert rec1_a.seq == 1
        assert rec1_b.seq == 2
        assert q1.empty()

        rec2_a = await asyncio.wait_for(q2.get(), timeout=1.0)
        rec2_b = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert rec2_a.seq == 1
        assert rec2_b.seq == 2
        assert q2.empty()

    # After exiting contexts, subscribers must be cleaned up
    assert await broadcaster.get_subscriber_count(task_id) == 0


def test_sanitize_tool_args() -> None:
    """Verify sensitive fields are masked and oversized strings truncated."""
    raw_args = {
        "api_key": "sk-secret-12345",
        "auth_token": "bearer-token-abc",
        "password": "supersecretpassword",
        "nested": {
            "secret_key": "nested-secret",
            "normal_field": "hello world",
        },
        "items": ["safe", {"token": "inside_list"}],
        "long_text": "A" * 1500,
    }
    sanitized = _sanitize_tool_args(raw_args)
    assert sanitized["api_key"] == "***"
    assert sanitized["auth_token"] == "***"
    assert sanitized["password"] == "***"
    assert sanitized["nested"]["secret_key"] == "***"
    assert sanitized["nested"]["normal_field"] == "hello world"
    assert sanitized["items"][0] == "safe"
    assert sanitized["items"][1]["token"] == "***"
    assert len(sanitized["long_text"]) < 1100
    assert sanitized["long_text"].endswith("... [truncated]")


@pytest.mark.asyncio
async def test_tool_executor_node_records_observability_events(tmp_path: Path) -> None:
    """Verify tool execution node records TOOL_STARTED and TOOL_COMPLETED events with task context."""
    cfg = _make_settings(tmp_path, "test_tool_events.db")
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    try:
        coord = rt.task_coordinator
        task = coord.create_task(query="Test tool execution observability")
        run = coord.start_run(task.task_id, runtime_id=rt.instance_id)

        registry = ToolRegistry()

        @tool
        def sample_search(query: str, api_key: str = "default_key") -> str:
            """Sample search tool."""
            return f"Search results for: {query}"

        registry.register(sample_search)
        executor_node = create_tool_executor_node(registry=registry)

        state: AgentState = {
            "messages": [
                HumanMessage(content="Search for quantum computing"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "sample_search",
                            "args": {"query": "quantum computing", "api_key": "secret_key_123"},
                            "id": "call_qc_001",
                        }
                    ],
                ),
            ],
            "run_context": {
                "task_id": task.task_id,
                "run_id": run.run_id,
                "session_id": "session_test",
            },
        }

        result = await executor_node(state)
        assert "messages" not in result or len(result.get("messages", [])) > 0

        # Verify events recorded in SQLite
        events = coord.list_events_for_task(task.task_id)
        event_types = [e.event_type for e in events]
        assert EventType.TOOL_STARTED in event_types
        assert EventType.TOOL_COMPLETED in event_types

        # Check TOOL_STARTED payload sanitization
        start_ev = next(e for e in events if e.event_type == EventType.TOOL_STARTED)
        start_payload = json.loads(start_ev.payload_json)
        assert start_payload["tool_name"] == "sample_search"
        assert start_payload["arguments"]["api_key"] == "***"
        assert start_payload["arguments"]["query"] == "quantum computing"

        # Check TOOL_COMPLETED payload
        comp_ev = next(e for e in events if e.event_type == EventType.TOOL_COMPLETED)
        comp_payload = json.loads(comp_ev.payload_json)
        assert comp_payload["tool_name"] == "sample_search"
        assert comp_payload["success"] is True
        assert "Search results for: quantum computing" in comp_payload["output_preview"]
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_sse_streaming_replay_and_last_event_id(tmp_path: Path) -> None:
    """Verify SSE stream endpoint supports since_seq and Last-Event-ID replay."""
    cfg = _make_settings(tmp_path, "test_sse_replay.db")
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/v1")

    try:
        coord = rt.task_coordinator
        task = coord.create_task(query="SSE Replay Task")
        run = coord.start_run(task.task_id, runtime_id=rt.instance_id)

        # Pre-record events 1, 2, 3
        e1 = coord.record_event(task.task_id, EventType.RUN_STARTED, run_id=run.run_id, payload={"step": 1})
        e2 = coord.record_event(task.task_id, EventType.PLAN_UPDATED, run_id=run.run_id, payload={"step": 2})
        e3 = coord.record_event(task.task_id, EventType.TASK_COMPLETED, run_id=run.run_id, payload={"step": 3})

        # Mark task as completed so stream finishes historical replay cleanly
        coord.complete_task_and_run(task.task_id, run.run_id)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Full replay from seq=0
            res = await client.get(f"/api/v1/tasks/{task.task_id}/events/stream")
            assert res.status_code == 200
            assert "text/event-stream" in res.headers["content-type"]
            body = res.text
            assert f"id: {e1.seq}" in body
            assert f"id: {e2.seq}" in body
            assert f"id: {e3.seq}" in body
            assert "run_started" in body
            assert "plan_updated" in body
            assert "task_completed" in body

            # 2. Replay with since_seq=2 (should only return e3)
            res2 = await client.get(f"/api/v1/tasks/{task.task_id}/events/stream?since_seq=2")
            assert res2.status_code == 200
            body2 = res2.text
            assert f"id: {e1.seq}" not in body2
            assert f"id: {e2.seq}" not in body2
            assert f"id: {e3.seq}" in body2

            # 3. Replay with Last-Event-ID header: "2"
            res3 = await client.get(
                f"/api/v1/tasks/{task.task_id}/events/stream",
                headers={"Last-Event-ID": "2"},
            )
            assert res3.status_code == 200
            body3 = res3.text
            assert f"id: {e1.seq}" not in body3
            assert f"id: {e2.seq}" not in body3
            assert f"id: {e3.seq}" in body3

            # 4. Stream non-existent task returns 404
            res4 = await client.get("/api/v1/tasks/task_nonexistent/events/stream")
            assert res4.status_code == 404
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_sse_streaming_live_push(tmp_path: Path) -> None:
    """Verify active SSE stream receives live events broadcast during execution."""
    cfg = _make_settings(tmp_path, "test_sse_live.db")
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/v1")

    try:
        coord = rt.task_coordinator
        task = coord.create_task(query="SSE Live Push Task")
        run = coord.start_run(task.task_id, runtime_id=rt.instance_id)

        coord.record_event(task.task_id, EventType.RUN_STARTED, run_id=run.run_id)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # We initiate streaming connection
            received_events: list[str] = []

            async def listen_stream() -> None:
                async with client.stream("GET", f"/api/v1/tasks/{task.task_id}/events/stream") as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("event:"):
                            received_events.append(line.split(":", 1)[1].strip())
                        if line.startswith("event: task_completed"):
                            break

            stream_task = asyncio.create_task(listen_stream())
            # Give client a moment to connect and subscribe
            await asyncio.sleep(0.1)

            # Broadcast live events while client is listening
            coord.record_event(
                task.task_id, EventType.TOOL_STARTED, run_id=run.run_id, payload={"tool": "search"}
            )
            await asyncio.sleep(0.05)
            coord.record_event(
                task.task_id, EventType.TOOL_COMPLETED, run_id=run.run_id, payload={"tool": "search"}
            )
            await asyncio.sleep(0.05)
            coord.record_event(
                task.task_id, EventType.TASK_COMPLETED, run_id=run.run_id, payload={"done": True}
            )

            # Wait for stream_task to finish reading the terminal event
            await asyncio.wait_for(stream_task, timeout=3.0)

            assert "run_started" in received_events
            assert "tool_started" in received_events
            assert "tool_completed" in received_events
            assert "task_completed" in received_events
    finally:
        await rt.shutdown()
