"""Tests for Sprint 1A.1 Authorization Security Patch: One-time consumption and Thread isolation."""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from jarvis_studio.agent.hitl.authorization import (
    verify_tool_execution_authorization,
)
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    get_hitl_manager,
)
from jarvis_studio.agent.nodes.executor import create_tool_executor_node
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def clean_hitl_state():
    """Ensure a clean HITL manager state before and after each test."""
    mgr = get_hitl_manager()
    mgr.clear()
    yield
    mgr.clear()


@pytest.fixture
def patch_registry():
    """ToolRegistry with dangerous tools for testing."""
    reg = ToolRegistry()
    reg.register(execute_shell_command)
    return reg


# -------------------------------------------------------------------------
# 1. Authorization Consumption Tests (1-6)
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_01_normal_approval_executes_once_and_consumes(patch_registry):
    """Test 1: Normal approval executes successfully once and transitions to CONSUMED."""
    hitl = get_hitl_manager()
    thread_id = "thread_c01"
    call_id = "call_c01"
    raw_args = {"command": "echo once"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_01",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))
    assert req.status == ApprovalStatus.APPROVED

    executor = create_tool_executor_node(patch_registry)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "execute_shell_command", "args": raw_args}],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    result = await executor(state)
    tool_msgs = result["messages"]
    assert len(tool_msgs) == 1
    assert "once" in tool_msgs[0].content

    # Request must now be in CONSUMED status
    updated_req = hitl.get_request(req.id)
    assert updated_req.status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_02_replay_consumed_authorization_is_blocked(patch_registry):
    """Test 2: Replay of an already CONSUMED authorization must be rejected."""
    hitl = get_hitl_manager()
    thread_id = "thread_c02"
    call_id = "call_c02"
    raw_args = {"command": "echo replay"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_02",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))

    executor = create_tool_executor_node(patch_registry)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "execute_shell_command", "args": raw_args}],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    # 1. First execution: succeeds and consumes
    res1 = await executor(state)
    assert "replay" in res1["messages"][0].content
    assert hitl.get_request(req.id).status == ApprovalStatus.CONSUMED

    # 2. Second execution attempt with identical payload: must be strictly BLOCKED
    res2 = await executor(state)
    assert len(res2["messages"]) == 1
    msg2 = res2["messages"][0]
    assert "Operation cancelled" in msg2.content
    assert "CONSUMED" in msg2.content or "already" in msg2.content or "not received valid approval" in msg2.content


@pytest.mark.asyncio
async def test_03_concurrent_executor_race_only_one_acquires(patch_registry):
    """Test 3: Two concurrent Executors attempting to execute the same approved tool call.

    Exactly ONE must acquire execution rights, and the other must be blocked.
    """
    hitl = get_hitl_manager()
    thread_id = "thread_c03"
    call_id = "call_c03"
    raw_args = {"command": "echo race"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_03",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))

    executor = create_tool_executor_node(patch_registry)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "execute_shell_command", "args": raw_args}],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    # Launch both concurrently
    results = await asyncio.gather(executor(state), executor(state), return_exceptions=True)

    outputs = []
    for r in results:
        assert isinstance(r, dict)
        outputs.append(r["messages"][0].content)

    # Exactly one succeeded and one was blocked
    success_count = sum(1 for o in outputs if "race" in o and "cancelled" not in o)
    blocked_count = sum(1 for o in outputs if "Operation cancelled" in o)
    assert success_count == 1
    assert blocked_count == 1

    # Request ends in CONSUMED
    assert hitl.get_request(req.id).status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_04_two_different_tool_calls_have_independent_leases(patch_registry):
    """Test 4: Two different tool calls in same turn have independent leases and both consume."""
    hitl = get_hitl_manager()
    thread_id = "thread_c04"
    call_id_1 = "call_c04_1"
    call_id_2 = "call_c04_2"
    args_1 = {"command": "echo first"}
    args_2 = {"command": "echo second"}

    req1 = hitl.create_request(
        thread_id=thread_id,
        run_id="run_04",
        tool_name="execute_shell_command",
        arguments=args_1,
        tool_call_id=call_id_1,
    )
    req2 = hitl.create_request(
        thread_id=thread_id,
        run_id="run_04",
        tool_name="execute_shell_command",
        arguments=args_2,
        tool_call_id=call_id_2,
    )
    hitl.resolve_request(req1.id, ApprovalDecision(decision="approve"))
    hitl.resolve_request(req2.id, ApprovalDecision(decision="approve"))

    executor = create_tool_executor_node(patch_registry)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[
                    {"id": call_id_1, "name": "execute_shell_command", "args": args_1},
                    {"id": call_id_2, "name": "execute_shell_command", "args": args_2},
                ],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id_1: {
                    "approval_id": req1.id,
                    "tool_call_id": call_id_1,
                    "decision": "approve",
                    "arguments_hash": req1.arguments_hash,
                },
                call_id_2: {
                    "approval_id": req2.id,
                    "tool_call_id": call_id_2,
                    "decision": "approve",
                    "arguments_hash": req2.arguments_hash,
                },
            }
        },
    }

    result = await executor(state)
    tool_msgs = result["messages"]
    assert len(tool_msgs) == 2
    assert "first" in tool_msgs[0].content
    assert "second" in tool_msgs[1].content

    # Both independent requests must be CONSUMED
    assert hitl.get_request(req1.id).status == ApprovalStatus.CONSUMED
    assert hitl.get_request(req2.id).status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_05_execution_exception_transitions_to_failed_and_blocks_replay():
    """Test 5: If tool execution raises an exception, status transitions to FAILED and replay is blocked."""
    reg = ToolRegistry()

    @tool
    def exploding_tool(arg: str) -> str:
        """A tool that always crashes."""
        raise RuntimeError("BOOM_EXPLOSION")

    tag_tool_safety(exploding_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg.register(exploding_tool)

    hitl = get_hitl_manager()
    thread_id = "thread_c05"
    call_id = "call_c05"
    raw_args = {"arg": "danger"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_05",
        tool_name="exploding_tool",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))

    executor = create_tool_executor_node(reg)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "exploding_tool", "args": raw_args}],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    # Execute -> tool raises RuntimeError
    result = await executor(state)
    tool_msgs = result["messages"]
    assert "BOOM_EXPLOSION" in tool_msgs[0].content

    # Status must transition to FAILED
    updated_req = hitl.get_request(req.id)
    assert updated_req.status == ApprovalStatus.FAILED

    # Attempting to replay this failed authorization must be blocked
    auth = verify_tool_execution_authorization(
        tool_name="exploding_tool",
        tool_call_id=call_id,
        raw_args=raw_args,
        state=state,
        registry=reg,
    )
    assert auth.is_authorized is False
    assert "FAILED" in auth.rejection_reason or "not received valid approval" in auth.rejection_reason


def test_06_consumed_authorization_cannot_be_reactivated_via_modify():
    """Test 6: A CONSUMED authorization cannot be re-resolved or reactivated via modify."""
    hitl = get_hitl_manager()
    req = hitl.create_request(
        thread_id="thread_c06",
        run_id="run_06",
        tool_name="execute_shell_command",
        arguments={"command": "ls"},
        tool_call_id="call_c06",
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))

    # Acquire and consume
    success, _acquired_req, _err = hitl.acquire_execution_lease(
        approval_id=req.id,
        thread_id="thread_c06",
        tool_call_id="call_c06",
        arguments_hash=req.arguments_hash,
    )
    assert success is True
    hitl.finalize_execution(req.id, success=True)
    assert req.status == ApprovalStatus.CONSUMED

    # Attempt to resolve again with 'modify' must be rejected
    with pytest.raises(ValueError, match="already been resolved"):
        hitl.resolve_request(
            req.id,
            ApprovalDecision(decision="modify", modified_arguments={"command": "cat /etc/passwd"}),
        )


# -------------------------------------------------------------------------
# 2. Thread Isolation Tests (7-10)
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_07_different_threads_same_tool_call_id_strictly_isolated(patch_registry):
    """Test 7: Two different threads with the same tool_call_id cannot share or steal authorization."""
    hitl = get_hitl_manager()
    call_id = "call_identical_id"
    raw_args = {"command": "echo thread_isolated"}

    # Thread A gets an approval request and user approves it
    req_a = hitl.create_request(
        thread_id="thread_A",
        run_id="run_A",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req_a.id, ApprovalDecision(decision="approve"))

    executor = create_tool_executor_node(patch_registry)

    # Thread B tries to execute using Thread A's approval grant or relying on manager fallback
    state_b = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "execute_shell_command", "args": raw_args}],
            )
        ],
        "run_context": {"session_id": "thread_B"},  # Different thread!
        # Thread B tries to reference req_a
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req_a.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req_a.arguments_hash,
                }
            }
        },
    }

    result_b = await executor(state_b)
    msg_b = result_b["messages"][0]
    # Thread B MUST be blocked due to Thread mismatch!
    assert "Operation cancelled" in msg_b.content
    assert (
        "Thread mismatch" in msg_b.content
        or "not received valid approval" in msg_b.content
        or "belongs to another thread" in msg_b.content
    )

    # Thread A's request must NOT have been consumed by Thread B
    req_a_check = hitl.get_request(req_a.id)
    assert req_a_check.status == ApprovalStatus.APPROVED

    # Now Thread A executes: should succeed
    state_a = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "execute_shell_command", "args": raw_args}],
            )
        ],
        "run_context": {"session_id": "thread_A"},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req_a.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req_a.arguments_hash,
                }
            }
        },
    }
    result_a = await executor(state_a)
    assert "thread_isolated" in result_a["messages"][0].content
    assert hitl.get_request(req_a.id).status == ApprovalStatus.CONSUMED


def test_08_same_thread_same_call_id_different_hash_blocked(patch_registry):
    """Test 8: Even within the same thread and tool_call_id, different arguments hash is blocked."""
    hitl = get_hitl_manager()
    thread_id = "thread_08"
    call_id = "call_08"

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_08",
        tool_name="execute_shell_command",
        arguments={"command": "echo safe"},
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"))

    state = {
        "messages": [],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    # Present tampered raw args
    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id=call_id,
        raw_args={"command": "rm -rf /"},
        state=state,
        registry=patch_registry,
    )
    assert auth.is_authorized is False
    assert "arguments do not match" in auth.rejection_reason or "mismatch" in auth.rejection_reason


def test_09_missing_thread_id_fails_closed(patch_registry):
    """Test 9: Execution context missing thread_id cannot execute any high-risk tool."""
    state_no_thread = {
        "messages": [],
        "run_context": {},  # Empty run_context, no session_id
        "approval_decision": {
            "tool_call_id": "call_09",
            "decision": "approve",
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id="call_09",
        raw_args={"command": "echo test"},
        state=state_no_thread,
        registry=patch_registry,
    )
    assert auth.is_authorized is False
    assert "thread" in auth.rejection_reason.lower() or "missing" in auth.rejection_reason.lower()


def test_10_full_lifecycle_thread_binding(patch_registry):
    """Test 10: Thread binding is strictly enforced across create -> resolve -> lease acquisition."""
    hitl = get_hitl_manager()
    thread_a = "thread_life_A"
    thread_b = "thread_life_B"
    call_id = "call_life_10"
    raw_args = {"command": "echo lifecycle"}

    # 1. Create on Thread A
    req = hitl.create_request(
        thread_id=thread_a,
        run_id="run_10",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    assert req.thread_id == thread_a

    # 2. Querying on Thread B with strict thread_id returns None
    assert hitl.get_by_tool_call_id(call_id, thread_id=thread_b) is None
    assert hitl.get_by_tool_call_id(call_id, thread_id=thread_a) is not None

    # 3. Resolve request with thread_id verification
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_a)
    assert req.status == ApprovalStatus.APPROVED

    # 4. Thread B cannot acquire execution lease
    acquired_b, _, err_b = hitl.acquire_execution_lease(
        approval_id=req.id,
        thread_id=thread_b,
        tool_call_id=call_id,
        arguments_hash=req.arguments_hash,
    )
    assert acquired_b is False
    assert "Thread mismatch" in err_b

    # 5. Thread A acquires lease -> status becomes EXECUTING
    acquired_a, leased_req, _ = hitl.acquire_execution_lease(
        approval_id=req.id,
        thread_id=thread_a,
        tool_call_id=call_id,
        arguments_hash=req.arguments_hash,
    )
    assert acquired_a is True
    assert leased_req.status == ApprovalStatus.EXECUTING

    # 6. Finalize -> CONSUMED
    final_req = hitl.finalize_execution(req.id, success=True)
    assert final_req.status == ApprovalStatus.CONSUMED
