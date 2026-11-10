"""Comprehensive security tests for Human-in-the-Loop (HITL) authorization and execution."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.authorization import (
    verify_tool_execution_authorization,
)
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    compute_arguments_hash,
    get_hitl_manager,
)
from jarvis_studio.agent.nodes.executor import create_tool_executor_node
from jarvis_studio.tools.builtin.file_tools import read_file, write_file
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


class MockToolCallingChatModel(FakeMessagesListChatModel):
    """Fake chat model capable of returning predefined messages with tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


def is_graph_interrupted(snapshot) -> bool:
    """Check if graph execution is currently interrupted in LangGraph."""
    return bool(len(snapshot.next) > 0 or any(t.interrupts for t in snapshot.tasks))


@pytest.fixture(autouse=True)
def clean_hitl_state():
    """Ensure a clean HITL manager state before and after each test."""
    mgr = get_hitl_manager()
    mgr.clear()
    yield
    mgr.clear()


@pytest.fixture
def secured_registry():
    """ToolRegistry with both safe and dangerous tools registered."""
    reg = ToolRegistry()
    reg.register(execute_shell_command)  # HIGH risk, requires approval
    reg.register(write_file)  # HIGH risk, requires approval
    reg.register(read_file)  # SAFE
    return reg


@pytest.mark.asyncio
async def test_01_single_high_risk_tool_normal_approval(secured_registry):
    """Test 1: Single high-risk tool call is paused and executes upon approval."""
    call_msg = AIMessage(
        content="Running shell command.",
        tool_calls=[
            {
                "id": "call_shell_001",
                "name": "execute_shell_command",
                "args": {"command": "echo test_01"},
            }
        ],
    )
    final_msg = AIMessage(content="Shell execution completed.")
    model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model, registry=secured_registry, checkpointer=checkpointer
    )

    thread_id = "test_thread_01"
    config = {"configurable": {"thread_id": thread_id}}

    # 1. Trigger run -> pauses at approval_gate
    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="run echo")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    snapshot = await graph.aget_state(config)
    assert is_graph_interrupted(snapshot)

    hitl = get_hitl_manager()
    pending = hitl.list_pending(thread_id)
    assert len(pending) == 1
    assert pending[0].tool_call_id == "call_shell_001"
    assert pending[0].arguments_hash == compute_arguments_hash({"command": "echo test_01"})

    # 2. Resume with approval
    resume_cmd = Command(
        resume={
            "tool_call_id": "call_shell_001",
            "decision": "approve",
            "reason": "Test approval",
        }
    )
    resumed_result = await graph.ainvoke(resume_cmd, config=config)
    final_snapshot = await graph.aget_state(config)
    assert not is_graph_interrupted(final_snapshot)

    # Verify tool was executed
    messages = resumed_result["messages"]
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_shell_001"
    assert "test_01" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_02_two_high_risk_tools_approved_separately(secured_registry):
    """Test 2: Two high-risk tools intercepted and approved sequentially in separate interrupts."""
    file_path = "test_02.txt"
    call_msg = AIMessage(
        content="Running two operations.",
        tool_calls=[
            {
                "id": "call_sh_01",
                "name": "execute_shell_command",
                "args": {"command": "echo seq_01"},
            },
            {
                "id": "call_wf_02",
                "name": "write_file",
                "args": {"file_path": file_path, "content": "hello_02"},
            },
        ],
    )
    final_msg = AIMessage(content="Both finished.")
    model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model, registry=secured_registry, checkpointer=checkpointer
    )

    thread_id = "test_thread_02"
    config = {"configurable": {"thread_id": thread_id}}

    # 1. First invocation -> interrupts on first tool
    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="run two tasks")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )
    snapshot = await graph.aget_state(config)
    assert is_graph_interrupted(snapshot)

    hitl = get_hitl_manager()
    pending = hitl.list_pending(thread_id)
    assert len(pending) == 2

    # 2. Resume first tool
    await graph.ainvoke(
        Command(resume={"tool_call_id": "call_sh_01", "decision": "approve"}),
        config=config,
    )

    # 3. Graph pauses again for the second tool
    snapshot2 = await graph.aget_state(config)
    assert is_graph_interrupted(snapshot2)
    pending2 = hitl.list_pending(thread_id)
    assert len(pending2) == 1
    assert pending2[0].tool_call_id == "call_wf_02"

    # 4. Resume second tool
    resumed = await graph.ainvoke(
        Command(resume={"tool_call_id": "call_wf_02", "decision": "approve"}),
        config=config,
    )

    final_snapshot = await graph.aget_state(config)
    assert not is_graph_interrupted(final_snapshot)

    tool_msgs = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    assert "seq_01" in tool_msgs[0].content
    assert "Successfully wrote" in tool_msgs[1].content


@pytest.mark.asyncio
async def test_03_first_approved_second_rejected(secured_registry):
    """Test 3: First high-risk tool approved, second rejected -> first runs, second blocked."""
    file_path = "test_03.txt"
    call_msg = AIMessage(
        content="Running two ops.",
        tool_calls=[
            {
                "id": "call_sh_03",
                "name": "execute_shell_command",
                "args": {"command": "echo approved_03"},
            },
            {
                "id": "call_wf_03",
                "name": "write_file",
                "args": {"file_path": file_path, "content": "danger_03"},
            },
        ],
    )
    final_msg = AIMessage(content="Finished after rejection.")
    model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model, registry=secured_registry, checkpointer=checkpointer
    )

    thread_id = "test_thread_03"
    config = {"configurable": {"thread_id": thread_id}}

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="run 03")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    # Approve first
    await graph.ainvoke(
        Command(resume={"tool_call_id": "call_sh_03", "decision": "approve"}),
        config=config,
    )

    # Reject second
    resumed = await graph.ainvoke(
        Command(
            resume={
                "tool_call_id": "call_wf_03",
                "decision": "reject",
                "reason": "Forbidden file write",
            }
        ),
        config=config,
    )

    tool_msgs = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    # First ran successfully
    assert "approved_03" in tool_msgs[0].content
    # Second was cancelled and blocked
    assert "Operation cancelled" in tool_msgs[1].content
    assert "Forbidden file write" in tool_msgs[1].content


@pytest.mark.asyncio
async def test_04_first_rejected_second_approved(secured_registry):
    """Test 4: First high-risk tool rejected, second approved -> first blocked, second runs."""
    file_path = "test_04.txt"
    call_msg = AIMessage(
        content="Running two ops.",
        tool_calls=[
            {
                "id": "call_sh_04",
                "name": "execute_shell_command",
                "args": {"command": "echo denied_04"},
            },
            {
                "id": "call_wf_04",
                "name": "write_file",
                "args": {"file_path": file_path, "content": "allowed_04"},
            },
        ],
    )
    final_msg = AIMessage(content="Finished after rejection.")
    model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model, registry=secured_registry, checkpointer=checkpointer
    )

    thread_id = "test_thread_04"
    config = {"configurable": {"thread_id": thread_id}}

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="run 04")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    # Reject first
    await graph.ainvoke(
        Command(
            resume={
                "tool_call_id": "call_sh_04",
                "decision": "reject",
                "reason": "No shell allowed",
            }
        ),
        config=config,
    )

    # Approve second
    resumed = await graph.ainvoke(
        Command(resume={"tool_call_id": "call_wf_04", "decision": "approve"}),
        config=config,
    )

    tool_msgs = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    # First was blocked
    assert "Operation cancelled" in tool_msgs[0].content
    assert "No shell allowed" in tool_msgs[0].content
    # Second ran successfully
    assert "Successfully wrote" in tool_msgs[1].content


@pytest.mark.asyncio
async def test_05_multiple_tool_calls_concurrent_batch_resolution(secured_registry):
    """Test 5: Multiple ToolCalls returned concurrently and resolved together via batch payload."""
    call_msg = AIMessage(
        content="Running three ops.",
        tool_calls=[
            {
                "id": "call_b1",
                "name": "execute_shell_command",
                "args": {"command": "echo b1"},
            },
            {
                "id": "call_b2",
                "name": "execute_shell_command",
                "args": {"command": "echo b2"},
            },
            {
                "id": "call_safe",
                "name": "read_file",
                "args": {"file_path": "non_existent.txt"},
            },
        ],
    )
    final_msg = AIMessage(content="Batch finished.")
    model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model, registry=secured_registry, checkpointer=checkpointer
    )

    thread_id = "test_thread_05"
    config = {"configurable": {"thread_id": thread_id}}

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="run batch")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    # Batch resume: approve call_b1, reject call_b2
    batch_resume = {
        "decisions": [
            {"tool_call_id": "call_b1", "decision": "approve"},
            {
                "tool_call_id": "call_b2",
                "decision": "reject",
                "reason": "b2 cancelled",
            },
        ]
    }
    resumed = await graph.ainvoke(Command(resume=batch_resume), config=config)

    tool_msgs = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 3
    # b1 executed
    assert "b1" in tool_msgs[0].content
    # b2 was blocked
    assert "Operation cancelled" in tool_msgs[1].content
    assert "b2 cancelled" in tool_msgs[1].content
    # call_safe executed without requiring approval
    assert "Error: File does not exist" in tool_msgs[2].content


@pytest.mark.asyncio
async def test_06_modify_first_tool_arguments_does_not_affect_second(
    secured_registry,
):
    """Test 6: Modifying first tool's arguments does not contaminate the second tool's arguments."""
    call_msg = AIMessage(
        content="Running two commands.",
        tool_calls=[
            {
                "id": "call_mod_1",
                "name": "execute_shell_command",
                "args": {"command": "echo original_1"},
            },
            {
                "id": "call_mod_2",
                "name": "execute_shell_command",
                "args": {"command": "echo original_2"},
            },
        ],
    )
    final_msg = AIMessage(content="Finished modify test.")
    model = MockToolCallingChatModel(responses=[call_msg, final_msg])
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model, registry=secured_registry, checkpointer=checkpointer
    )

    thread_id = "test_thread_06"
    config = {"configurable": {"thread_id": thread_id}}

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="run 06")],
            "run_context": {"session_id": thread_id},
        },
        config=config,
    )

    # Resume with modified arguments for call_mod_1, and standard approval for call_mod_2
    batch_resume = {
        "decisions": [
            {
                "tool_call_id": "call_mod_1",
                "decision": "modify",
                "modified_arguments": {"command": "echo modified_1"},
            },
            {
                "tool_call_id": "call_mod_2",
                "decision": "approve",
            },
        ]
    }
    resumed = await graph.ainvoke(Command(resume=batch_resume), config=config)

    tool_msgs = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    # First ran with MODIFIED command
    assert "modified_1" in tool_msgs[0].content
    assert "original_1" not in tool_msgs[0].content
    # Second ran with its OWN ORIGINAL command (uncontaminated)
    assert "original_2" in tool_msgs[1].content
    assert "modified_1" not in tool_msgs[1].content


def test_07_duplicate_submission_rejected():
    """Test 7: Resubmitting a decision for an already resolved request raises ValueError (idempotency)."""
    hitl = get_hitl_manager()
    req = hitl.create_request(
        thread_id="t_07",
        run_id="r_07",
        tool_name="execute_shell_command",
        arguments={"command": "ls"},
        tool_call_id="call_dup_07",
    )

    # First resolution: OK
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))
    assert req.status == ApprovalStatus.APPROVED

    # Second resolution: must raise ValueError
    with pytest.raises(ValueError, match="has already been resolved"):
        hitl.resolve_request(req.id, ApprovalDecision(decision="reject"))


@pytest.mark.asyncio
async def test_08_unapproved_tool_directly_entering_executor_is_blocked(
    secured_registry,
):
    """Test 8: If a high-risk tool call reaches the executor without prior approval, it is strictly blocked."""
    executor = create_tool_executor_node(secured_registry)

    # State containing dangerous tool call, but NO approval_decision
    unapproved_state = {
        "messages": [
            AIMessage(
                content="Bypassing gate directly.",
                tool_calls=[
                    {
                        "id": "call_evil_08",
                        "name": "execute_shell_command",
                        "args": {"command": "echo evil"},
                    }
                ],
            )
        ],
        "run_context": {"session_id": "thread_08"},
        "approval_decision": None,
    }

    result = await executor(unapproved_state)
    tool_msgs = result["messages"]
    assert len(tool_msgs) == 1
    # Must be blocked by authorization gatekeeper
    assert "Operation cancelled" in tool_msgs[0].content
    assert "has not received valid approval" in tool_msgs[0].content


def test_09_tool_call_id_mismatch_rejected(secured_registry):
    """Test 9: Approval decision with mismatched tool_call_id fails authorization."""
    state = {
        "messages": [],
        "run_context": {"session_id": "thread_09"},
        "approval_decision": {
            "tool_call_id": "call_authorized_AAA",
            "decision": "approve",
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id="call_victim_BBB",  # Mismatched ID!
        raw_args={"command": "echo test"},
        state=state,
        registry=secured_registry,
    )

    assert auth.is_authorized is False
    assert auth.status == "unauthorized_mismatch"
    assert "does not match" in auth.rejection_reason


def test_10_tampered_arguments_hash_mismatch_rejected(secured_registry):
    """Test 10: Approval granted for safe command cannot be applied to tampered dangerous command."""
    original_args = {"command": "echo safe_args"}
    tampered_args = {"command": "rm -rf / --no-preserve-root"}

    state = {
        "messages": [],
        "run_context": {"session_id": "thread_10"},
        "approval_decision": {
            "tool_call_id": "call_tamper_10",
            "decision": "approve",
            "arguments_hash": compute_arguments_hash(original_args),
        },
    }

    # Verify authorization with tampered arguments
    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id="call_tamper_10",
        raw_args=tampered_args,  # Tampered!
        state=state,
        registry=secured_registry,
    )

    assert auth.is_authorized is False
    assert auth.status == "unauthorized_mismatch"
    assert "Original tool arguments do not match approved request" in auth.rejection_reason
