"""Unit tests for business-level thinking engine, route guard, and trace logging."""

import pytest

from jarvis_studio.core.guard.route_guard import GuardAction, IntentRouteGuard
from jarvis_studio.core.observability.trace_logger import (
    ConversationTrace,
    TraceStore,
)
from jarvis_studio.core.thinking.engine import (
    ThinkingEngine,
    ThinkingPhase,
)


@pytest.mark.asyncio
async def test_thinking_engine_phases():
    """Verify 4-stage thinking engine progression and white-box labels."""
    engine = ThinkingEngine(session_id="test_sess_01", heartbeat_interval=0.2)

    # Phase 1: Understand
    await engine.emit_understand()
    events = await engine.get_queued_events()
    assert len(events) == 1
    assert events[0]["event"] == "thinking"
    assert events[0]["data"]["phase"] == ThinkingPhase.UNDERSTAND.value
    assert events[0]["data"]["status"] == "start"

    # Phase 2: Tool
    await engine.emit_tool_start("read_file", {"path": "test.py"})
    await engine.emit_tool_end("read_file", "file contents")
    tool_events = await engine.get_queued_events()
    assert len(tool_events) == 2
    assert "正在读取工作区源码文件" in tool_events[0]["data"]["text"]
    assert tool_events[1]["data"]["text"].startswith("✓")

    # Phase 4: Completed
    await engine.emit_completed()
    comp_events = await engine.get_queued_events()
    assert len(comp_events) == 1
    assert comp_events[0]["data"]["status"] == "completed"
    engine.close()


def test_intent_route_guard_fast_path():
    """Verify <50ms direct rule matching without LLM tokens."""
    guard = IntentRouteGuard()

    # 1. Greetings
    res_hi = guard.evaluate("你好", "sess_01")
    assert res_hi.action == GuardAction.DIRECT_REPLY
    assert "AgentStudio" in res_hi.reply_text

    # 2. Identity
    res_id = guard.evaluate("你是谁？", "sess_01")
    assert res_id.action == GuardAction.DIRECT_REPLY
    assert "AgentStudio" in res_id.reply_text

    # 3. Ping
    res_ping = guard.evaluate("ping", "sess_01")
    assert res_ping.action == GuardAction.DIRECT_REPLY
    assert "pong" in res_ping.reply_text

    # 4. Emergency halt
    res_halt = guard.evaluate("停止！", "sess_01")
    assert res_halt.action == GuardAction.HALT_TASK

    # 5. Regular complex task
    res_complex = guard.evaluate("帮我用 FastAPI 写一个用户认证接口", "sess_01")
    assert res_complex.action == GuardAction.PASS_THROUGH


def test_intent_route_guard_sticky_anchor():
    """Verify sticky context anchors file entities across multi-turn queries."""
    guard = IntentRouteGuard()
    sess_id = "sess_sticky_test"

    # Round 1: Mentions a file
    r1 = guard.evaluate("请分析一下 src/jarvis_studio/api/app.py 的架构设计", sess_id)
    assert r1.action == GuardAction.PASS_THROUGH
    sticky = guard.get_sticky_context(sess_id)
    assert sticky.get("active_file") == "src/jarvis_studio/api/app.py"

    # Round 2: Vague follow-up
    r2 = guard.evaluate("看一下它的第 20 行", sess_id)
    assert r2.action == GuardAction.ENRICH_INTENT
    assert "src/jarvis_studio/api/app.py" in r2.enriched_message


def test_trace_store_lifecycle_metrics():
    """Verify 5-stage trace recording, 3D streaming metrics, and SQLite persistence."""
    store = TraceStore(db_path=":memory:")
    trace = ConversationTrace(
        session_id="sess_trace_test",
        run_id="run_001",
        query="Write unit test",
    )

    trace.mark_ttft()
    trace.mark_ttfa()
    trace.add_step(
        step_type="tool",
        name="read_file",
        arguments={"path": "test.py"},
        result="success",
        duration_ms=45,
    )
    trace.finish(output="Test completed successfully", status="completed")

    assert trace.ttft_ms is not None
    assert trace.ttfa_ms is not None
    assert trace.ttlt_ms is not None
    assert len(trace.thinking_steps) == 1

    store.save_trace(trace)

    traces = store.list_traces()
    assert len(traces) == 1
    assert traces[0]["trace_id"] == trace.trace_id

    detail = store.get_trace(trace.trace_id)
    assert detail is not None
    assert detail["output_text"] == "Test completed successfully"
    assert len(detail["thinking_steps"]) == 1


@pytest.mark.asyncio
async def test_thinking_engine_fingerprint_dedup_and_new_labels():
    """Verify consecutive identical thinking events are deduplicated and new tool labels exist."""
    engine = ThinkingEngine(session_id="test_dedup_01", heartbeat_interval=5.0)
    await engine.emit_understand("正在理解...")
    await engine.emit_understand("正在理解...")  # duplicate consecutive fingerprint
    await engine.emit_tool_start("search_web", {"query": "test"})
    await engine.emit_tool_start("search_web", {"query": "test2"})  # same phase|status|text

    events = await engine.get_queued_events()
    assert len(events) == 2
    assert events[0]["data"]["phase"] == ThinkingPhase.UNDERSTAND.value
    assert "正在联网检索相关资料" in events[1]["data"]["text"]
    engine.close()


def test_trace_sanitization_parallel_group_and_argument_alignment():
    """Verify secret redaction, parallel group tagging, and HITL argument alignment."""
    trace = ConversationTrace(
        session_id="sess_sec",
        run_id="run_sec",
        query="Check Bearer abcdefghij1234567890",
    )
    step = trace.add_step(
        step_type="mcp",
        name="github_search",
        arguments={"token": "secret_123", "query": "langgraph"},
        result="ok sk-1234567890abcdef1234",
        duration_ms=25,
        group="parallel_1",
    )
    assert step.group == "parallel_1"
    assert step.arguments["token"] == "[REDACTED]"
    assert "[REDACTED]" in step.result

    updated = trace.update_last_tool_arguments(
        "github_search", {"token": "new_secret", "query": "langgraph_v2"}
    )
    assert updated is True
    assert trace.thinking_steps[0].arguments["query"] == "langgraph_v2"
    assert trace.thinking_steps[0].arguments["token"] == "[REDACTED]"

    payload = trace.to_dict()
    assert "[REDACTED]" in payload["query"]


@pytest.mark.asyncio
async def test_iter_graph_with_thinking_flushes_heartbeat_during_silent_wait():
    """Verify heartbeats are yielded in real time while graph_stream is silent."""
    import asyncio

    from jarvis_studio.service.agent_service import AgentService

    engine = ThinkingEngine(session_id="hb_test", heartbeat_interval=0.05)
    engine.start_heartbeat()

    async def slow_graph():
        await asyncio.sleep(0.18)
        yield {"event": "on_chat_model_stream", "data": {}}

    seen: list[tuple[str, dict]] = []
    try:
        async for kind, ev in AgentService._iter_graph_with_thinking(slow_graph(), engine):
            seen.append((kind, ev))
    finally:
        engine.close()

    thinking_events = [ev for k, ev in seen if k == "thinking"]
    graph_events = [ev for k, ev in seen if k == "graph"]
    assert len(thinking_events) >= 1
    assert len(graph_events) == 1
    # Heartbeat arrived before the slow graph event
    first_think_idx = next(i for i, (k, _) in enumerate(seen) if k == "thinking")
    first_graph_idx = next(i for i, (k, _) in enumerate(seen) if k == "graph")
    assert first_think_idx < first_graph_idx

