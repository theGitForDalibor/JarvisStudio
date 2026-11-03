"""Unit tests for Server-Sent Events (SSE) streaming and Web UI delivery."""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from jarvis_studio.api.app import create_app
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.tools.registry import ToolRegistry


class StreamingMockChatModel:
    """Mock model producing streaming token chunks."""

    def __init__(self, chunks=None):
        self.chunks = chunks or ["Hello", ", ", "world", "!"]

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        full_text = "".join(self.chunks)
        return AIMessageChunk(content=full_text)

    async def astream(self, messages, **kwargs):
        for c in self.chunks:
            yield AIMessageChunk(content=c)


def test_web_ui_endpoint():
    """Verify /app serves the HTML Web application."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/app")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Jarvis Studio" in resp.text
        assert "messages-container" in resp.text


def test_chat_stream_api_endpoint():
    """Verify POST /api/v1/chat/stream returns text/event-stream content."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/chat/stream",
            json={"message": "hello", "session_id": "stream_test_01"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Stream contains SSE lines
        content = resp.text
        assert "event:" in content or "data:" in content


@pytest.mark.asyncio
async def test_agent_service_stream_generator():
    """Verify stream_agent yields token and completion events."""
    from langgraph.checkpoint.memory import MemorySaver

    from jarvis_studio.agent.graph import build_agent_graph

    mock_model = StreamingMockChatModel(chunks=["This", " is", " streaming."])
    checkpointer = MemorySaver()
    registry = ToolRegistry()

    graph = build_agent_graph(
        model=mock_model,
        registry=registry,
        checkpointer=checkpointer,
    )

    svc = AgentService(graph=graph)
    events = []
    async for ev in svc.stream_agent(
        thread_id="test_stream_thread", user_message="test"
    ):
        events.append(ev)

    assert len(events) > 0
    event_types = [e["event"] for e in events]
    assert "completed" in event_types
