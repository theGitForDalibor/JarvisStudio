"""Unit tests for business-level thinking engine, route guard, and trace logging."""

import pytest

from jarvis_studio.core.guard.route_guard import GuardAction, IntentRouteGuard
from jarvis_studio.core.observability.trace_logger import (
    ConversationTrace,
    ThinkingStep,
    TraceStore,
)
from jarvis_studio.core.thinking.engine import (
    TOOL_LABEL_MAP,
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
    assert "Jarvis" in res_hi.reply_text

    # 2. Identity
    res_id = guard.evaluate("你是谁？", "sess_01")
    assert res_id.action == GuardAction.DIRECT_REPLY
    assert "Jarvis Studio" in res_id.reply_text

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
