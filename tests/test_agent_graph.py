"""Integration tests for the LangGraph agent state machine and HITL interrupts."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    get_hitl_manager,
)
from jarvis_studio.tools.builtin.file_tools import read_file
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


class MockToolCallingChatModel(FakeMessagesListChatModel):
    """Custom fake model capable of producing tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_agent_graph_pure_chat():
    """Verify standard single-turn conversation with no tool invocations."""
    responses = [AIMessage(content="Hello, I am Jarvis. How can I help you today?")]
    fake_model = MockToolCallingChatModel(responses=responses)
    checkpointer = MemorySaver()
    registry = ToolRegistry()

    graph = build_agent_graph(
        model=fake_model,
        registry=registry,
        checkpointer=checkpointer,
    )

    thread_id = "test_thread_01"
    config = {"configurable": {"thread_id": thread_id}}

    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="Hello Jarvis!")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    assert len(result["messages"]) >= 2
    last_msg = result["messages"][-1]
    assert isinstance(last_msg, AIMessage)
    assert "Hello, I am Jarvis" in last_msg.content


@pytest.mark.asyncio
async def test_agent_graph_hitl_interrupt_and_approval():
    """Verify that dangerous tool calls trigger LangGraph interrupt and can be resumed."""
    # First response calls dangerous tool, second response gives final answer
    call_msg = AIMessage(
        content="I will run the command now.",
        tool_calls=[
            {
                "name": "execute_shell_command",
                "args": {"command": "echo test"},
                "id": "call_shell_001",
            }
        ],
    )
    final_msg = AIMessage(content="Shell execution finished.")

    fake_model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    registry = ToolRegistry()
    registry.register(execute_shell_command)

    graph = build_agent_graph(
        model=fake_model,
        registry=registry,
        checkpointer=checkpointer,
    )

    thread_id = "test_thread_hitl"
    config = {"configurable": {"thread_id": thread_id}}

    # 1. Trigger run -> should interrupt at approval_gate
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="Run echo test")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    state_snapshot = await graph.aget_state(config)
    assert len(state_snapshot.next) > 0  # Execution paused

    hitl_mgr = get_hitl_manager()
    pending = hitl_mgr.list_pending(thread_id)
    assert len(pending) == 1
    appr_req = pending[0]
    assert appr_req.tool_name == "execute_shell_command"
    assert appr_req.status == ApprovalStatus.PENDING

    # 2. Resume graph with Approval Decision
    resume_command = Command(
        resume={
            "decision": "approve",
            "reason": "Authorized by test",
        }
    )

    final_result = await graph.ainvoke(resume_command, config=config)

    # 3. Verify graph resumed and reached completion
    final_snapshot = await graph.aget_state(config)
    assert len(final_snapshot.next) == 0  # Reached END

    last_msg = final_result["messages"][-1]
    assert isinstance(last_msg, AIMessage)
    assert "Shell execution finished." in last_msg.content
