"""Security hardening tests for Sprint 1A.2:
Fix 1: Fail-Closed when approval_id is missing/empty/invalid on acquire_lease=True.
Fix 2: Deterministic asyncio.CancelledError cleanup and propagation in executor.
Fix 3: Thread isolation & trusted session validation in Approval API routes.
Fix 4: Remove 'default' thread fallback in approval_node (Fail-Closed).
Fix 5: Clear separation of tool execution success vs finalize_execution failure semantics.
Multi-threaded Barrier concurrency tests for HITLManager and Executor.
"""

import asyncio
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

import jarvis_studio.service.agent_service as agent_service_mod
import jarvis_studio.service.session_service as session_service_mod
from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.authorization import verify_tool_execution_authorization
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    compute_arguments_hash,
    get_hitl_manager,
)
from jarvis_studio.agent.nodes.approval import create_approval_node
from jarvis_studio.agent.nodes.executor import create_tool_executor_node
from jarvis_studio.api.app import create_app
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.service.session_service import SessionService
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety
from jarvis_studio.tools.builtin.file_tools import read_file
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


class _MockToolCallingChatModel(FakeMessagesListChatModel):
    """Fake model supporting tool binding."""

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture(autouse=True)
def _clean_hitl_singleton():
    """Reset HITLManager singleton before and after every test."""
    mgr = get_hitl_manager()
    mgr.clear()
    yield
    mgr.clear()


@pytest.fixture
def isolated_services(tmp_path: Path):
    """Provide isolated SessionService (temp SQLite) and AgentService for API tests."""
    old_ss = session_service_mod._global_session_service
    old_as = agent_service_mod._global_agent_service

    db_file = str(tmp_path / "test_sessions_1a2.db")
    test_ss = SessionService(db_path=db_file)
    session_service_mod._global_session_service = test_ss

    reg = ToolRegistry()
    reg.register(execute_shell_command)
    reg.register(read_file)

    test_as = AgentService(
        graph=build_agent_graph(
            model=_MockToolCallingChatModel(responses=[AIMessage(content="ok")]),
            registry=reg,
            checkpointer=MemorySaver(),
        )
    )
    agent_service_mod._global_agent_service = test_as

    try:
        yield test_ss, test_as, reg
    finally:
        session_service_mod._global_session_service = old_ss
        agent_service_mod._global_agent_service = old_as


# -------------------------------------------------------------------------
# Fix 1: Missing / Empty / Invalid approval_id MUST Fail-Closed
# -------------------------------------------------------------------------


def test_f1_01_missing_approval_id_fails_closed_even_if_request_in_manager():
    """acquire_lease=True without approval_id must Fail-Closed and NOT backfill or consume."""
    hitl = get_hitl_manager()
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    thread_id = "thread_f1_01"
    call_id = "call_f1_01"
    raw_args = {"command": "echo should_not_run"}

    # Even when a valid APPROVED request exists in HITLManager,
    # omitting approval_id in state decision must Fail-Closed and not consume it.
    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_f1",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    state = {
        "messages": [],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                    # approval_id deliberately missing!
                }
            }
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id=call_id,
        raw_args=raw_args,
        state=state,
        registry=reg,
        acquire_lease=True,
    )
    assert auth.is_authorized is False
    assert auth.status != "authorized_safe"
    # Reject path must NOT consume the stored request
    assert hitl.get_request(req.id).status == ApprovalStatus.APPROVED


def test_f1_01b_missing_approval_id_without_manager_record_fails_closed():
    """Injected state decision with valid hash but no approval_id and no HITL record must fail closed."""
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    raw_args = {"command": "echo bypass_attempt"}
    state = {
        "messages": [],
        "run_context": {"session_id": "thread_f1_bypass"},
        "approval_decision": {
            "decision": "approve",
            "arguments_hash": compute_arguments_hash(raw_args),
            # No approval_id, no HITL record in manager
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id="call_injected",
        raw_args=raw_args,
        state=state,
        registry=reg,
        acquire_lease=True,
    )
    assert auth.is_authorized is False
    assert "approval_id" in (auth.rejection_reason or "").lower() or "lease" in (
        auth.rejection_reason or ""
    ).lower()


@pytest.mark.parametrize("bad_appr_id", ["", "   "])
def test_f1_02_empty_or_whitespace_approval_id_fails_closed(bad_appr_id: str):
    """Empty or whitespace approval_id must Fail-Closed and never downgrade to safe."""
    hitl = get_hitl_manager()
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    thread_id = "thread_f1_02"
    call_id = "call_f1_02"
    raw_args = {"command": "echo empty_id"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_f1_02",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    state = {
        "messages": [],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": bad_appr_id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id=call_id,
        raw_args=raw_args,
        state=state,
        registry=reg,
        acquire_lease=True,
    )
    assert auth.is_authorized is False
    assert hitl.get_request(req.id).status == ApprovalStatus.APPROVED


def test_f1_03_nonexistent_approval_id_fails_closed():
    """Unknown approval_id must Fail-Closed without creating any request."""
    hitl = get_hitl_manager()
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    raw_args = {"command": "echo ghost"}
    state = {
        "messages": [],
        "run_context": {"session_id": "thread_f1_03"},
        "approval_decision": {
            "decisions_by_call_id": {
                "call_f1_03": {
                    "approval_id": "appr_ghost_9999",
                    "tool_call_id": "call_f1_03",
                    "decision": "approve",
                    "arguments_hash": compute_arguments_hash(raw_args),
                }
            }
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id="call_f1_03",
        raw_args=raw_args,
        state=state,
        registry=reg,
        acquire_lease=True,
    )
    assert auth.is_authorized is False
    assert hitl.list_pending() == []


def test_f1_04_other_thread_approval_id_fails_closed():
    """approval_id belonging to Thread A cannot authorize Thread B and remains APPROVED."""
    hitl = get_hitl_manager()
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    raw_args = {"command": "echo cross"}
    call_id = "call_f1_04"

    req_a = hitl.create_request(
        thread_id="thread_A",
        run_id="run_A",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req_a.id, ApprovalDecision(decision="approve"), thread_id="thread_A")

    # Even if Thread B omits arguments_hash and tries to leverage req_a.id
    state_b = {
        "messages": [],
        "run_context": {"session_id": "thread_B"},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req_a.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                }
            }
        },
    }

    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id=call_id,
        raw_args=raw_args,
        state=state_b,
        registry=reg,
        acquire_lease=True,
    )
    assert auth.is_authorized is False
    assert hitl.get_request(req_a.id).status == ApprovalStatus.APPROVED


def test_f1_05_valid_authorization_acquires_lease_and_executes():
    """Valid authorization with thread_id + tool_call_id + approval_id + hash still succeeds."""
    hitl = get_hitl_manager()
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    raw_args = {"command": "echo valid"}
    req = hitl.create_request(
        thread_id="thread_valid",
        run_id="run_valid",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id="call_valid",
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id="thread_valid")

    state = {
        "messages": [],
        "run_context": {"session_id": "thread_valid"},
        "approval_decision": {
            "decisions_by_call_id": {
                "call_valid": {
                    "approval_id": req.id,
                    "tool_call_id": "call_valid",
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }
    auth = verify_tool_execution_authorization(
        tool_name="execute_shell_command",
        tool_call_id="call_valid",
        raw_args=raw_args,
        state=state,
        registry=reg,
        acquire_lease=True,
    )
    assert auth.is_authorized is True
    assert auth.approval_id == req.id
    assert hitl.get_request(req.id).status == ApprovalStatus.EXECUTING


# -------------------------------------------------------------------------
# Fix 2 & Fix 5: CancelledError & finalize_execution Failure Semantics
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f2_01_cancelled_error_transitions_executing_to_failed_and_propagates():
    """Cancelling tool execution via asyncio.CancelledError finalizes EXECUTING -> FAILED and propagates."""
    reg = ToolRegistry()
    started = asyncio.Event()

    @tool
    async def cancellable_tool(arg: str) -> str:
        """Async tool that waits until cancelled."""
        started.set()
        await asyncio.Future()
        return f"done:{arg}"

    tag_tool_safety(cancellable_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg.register(cancellable_tool)

    hitl = get_hitl_manager()
    thread_id = "thread_f2_01"
    call_id = "call_f2_01"
    raw_args = {"arg": "test"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_f2_01",
        tool_name="cancellable_tool",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    executor = create_tool_executor_node(reg)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "cancellable_tool", "args": raw_args}],
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

    task = asyncio.create_task(executor(state))
    await started.wait()
    assert hitl.get_request(req.id).status == ApprovalStatus.EXECUTING

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Lease must NOT remain stuck in EXECUTING
    finalized = hitl.get_request(req.id)
    assert finalized.status == ApprovalStatus.FAILED
    assert finalized.execution_error is not None
    assert "cancel" in finalized.execution_error.lower()

    # Subsequent replay attempt must remain strictly blocked
    replay_res = await executor(state)
    assert "Operation cancelled" in replay_res["messages"][0].content


@pytest.mark.asyncio
async def test_f2_02_cancelled_error_propagates_even_if_finalize_execution_raises():
    """If finalize_execution raises during cancellation cleanup, CancelledError must STILL propagate."""
    reg = ToolRegistry()

    @tool
    async def immediate_cancel_tool(arg: str) -> str:
        """Tool raising CancelledError."""
        raise asyncio.CancelledError("Simulated task cancel")

    tag_tool_safety(immediate_cancel_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg.register(immediate_cancel_tool)

    hitl = get_hitl_manager()
    thread_id = "thread_f2_02"
    call_id = "call_f2_02"
    raw_args = {"arg": "x"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_f2_02",
        tool_name="immediate_cancel_tool",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    executor = create_tool_executor_node(reg)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "immediate_cancel_tool", "args": raw_args}],
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

    with (
        patch.object(hitl, "finalize_execution", side_effect=RuntimeError("Store unreachable")),
        pytest.raises(asyncio.CancelledError),
    ):
        await executor(state)


@pytest.mark.asyncio
async def test_f2_03_normal_tool_exception_still_transitions_to_failed():
    """Standard Exception during tool invocation still transitions EXECUTING -> FAILED."""
    reg = ToolRegistry()

    @tool
    def broken_tool(val: str) -> str:
        """Tool raising ValueError."""
        raise ValueError(f"broken:{val}")

    tag_tool_safety(broken_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg.register(broken_tool)

    hitl = get_hitl_manager()
    req = hitl.create_request(
        thread_id="thread_f2_03",
        run_id="run_f2_03",
        tool_name="broken_tool",
        arguments={"val": "bad"},
        tool_call_id="call_f2_03",
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id="thread_f2_03")

    executor = create_tool_executor_node(reg)
    res = await executor(
        {
            "messages": [
                AIMessage(
                    content="run",
                    tool_calls=[{"id": "call_f2_03", "name": "broken_tool", "args": {"val": "bad"}}],
                )
            ],
            "run_context": {"session_id": "thread_f2_03"},
            "approval_decision": {
                "decisions_by_call_id": {
                    "call_f2_03": {
                        "approval_id": req.id,
                        "tool_call_id": "call_f2_03",
                        "decision": "approve",
                        "arguments_hash": req.arguments_hash,
                    }
                }
            },
        }
    )
    assert "Error executing 'broken_tool': broken:bad" in res["messages"][0].content
    updated = hitl.get_request(req.id)
    assert updated.status == ApprovalStatus.FAILED
    assert "broken:bad" in (updated.execution_error or "")


@pytest.mark.asyncio
async def test_f2_04_normal_tool_execution_still_transitions_to_consumed():
    """Normal approved high-risk tool execution still transitions EXECUTING -> CONSUMED."""
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    hitl = get_hitl_manager()
    args = {"command": "echo f2_ok"}
    req = hitl.create_request(
        thread_id="thread_f2_04",
        run_id="run_f2_04",
        tool_name="execute_shell_command",
        arguments=args,
        tool_call_id="call_f2_04",
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id="thread_f2_04")

    executor = create_tool_executor_node(reg)
    res = await executor(
        {
            "messages": [
                AIMessage(
                    content="run",
                    tool_calls=[{"id": "call_f2_04", "name": "execute_shell_command", "args": args}],
                )
            ],
            "run_context": {"session_id": "thread_f2_04"},
            "approval_decision": {
                "decisions_by_call_id": {
                    "call_f2_04": {
                        "approval_id": req.id,
                        "tool_call_id": "call_f2_04",
                        "decision": "approve",
                        "arguments_hash": req.arguments_hash,
                    }
                }
            },
        }
    )
    assert "f2_ok" in res["messages"][0].content
    assert hitl.get_request(req.id).status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_f5_01_finalize_success_failure_preserves_output_and_blocks_replay():
    """If tool succeeds but finalize_execution fails, preserve tool output, clearly signal auth failure, and block replay."""
    reg = ToolRegistry()
    invocation_count = 0

    @tool
    def side_effect_tool(val: str) -> str:
        """Tool tracking exact number of executions."""
        nonlocal invocation_count
        invocation_count += 1
        return f"SIDE_EFFECT_DONE:{val}"

    tag_tool_safety(side_effect_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg.register(side_effect_tool)

    hitl = get_hitl_manager()
    thread_id = "thread_f5_01"
    call_id = "call_f5_01"
    raw_args = {"val": "important"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_f5_01",
        tool_name="side_effect_tool",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    executor = create_tool_executor_node(reg)
    state = {
        "messages": [
            AIMessage(
                content="test",
                tool_calls=[{"id": call_id, "name": "side_effect_tool", "args": raw_args}],
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

    finalize_calls: list[tuple[str, bool]] = []

    def failing_finalize(approval_id: str, success: bool, error: str | None = None):
        finalize_calls.append((approval_id, success))
        raise RuntimeError("Lease state backend timeout")

    with patch.object(hitl, "finalize_execution", side_effect=failing_finalize):
        res = await executor(state)

    # Tool actually executed ONCE
    assert invocation_count == 1
    # finalize_execution called ONLY ONCE with success=True (NOT retried with success=False)
    assert finalize_calls == [(req.id, True)]

    msg = res["messages"][0]
    assert isinstance(msg, ToolMessage)
    # 1. Actual tool output is preserved (NOT disguised as "Error executing 'side_effect_tool'")
    assert "SIDE_EFFECT_DONE:important" in msg.content
    assert not msg.content.startswith("Error executing")
    # 2. Clearly communicates authorization finalization error vs tool execution success
    assert "AUTHORIZATION_FINALIZATION_ERROR" in msg.content
    assert msg.additional_kwargs.get("tool_succeeded") is True
    assert "Lease state backend timeout" in msg.additional_kwargs.get(
        "authorization_finalization_error", ""
    )

    # 3. Lease remained EXECUTING, so re-execution is strictly blocked
    assert hitl.get_request(req.id).status == ApprovalStatus.EXECUTING
    replay_res = await executor(state)
    assert invocation_count == 1
    assert "Operation cancelled" in replay_res["messages"][0].content


# -------------------------------------------------------------------------
# Fix 4: Remove "default" Thread Identity Fallback in approval_node
# -------------------------------------------------------------------------


def test_f4_01_approval_node_missing_or_default_thread_fails_closed():
    """approval_node must Fail-Closed when session_id is missing, empty, or 'default'."""
    hitl = get_hitl_manager()
    reg = ToolRegistry()
    reg.register(execute_shell_command)

    approval_node = create_approval_node(reg)

    for bad_ctx in [
        {},
        {"session_id": None},
        {"session_id": ""},
        {"session_id": "   "},
        {"session_id": "default"},
    ]:
        state = {
            "messages": [
                AIMessage(
                    content="Need shell",
                    tool_calls=[
                        {
                            "id": "call_no_tid",
                            "name": "execute_shell_command",
                            "args": {"command": "echo dangerous"},
                        }
                    ],
                )
            ],
            "run_context": bad_ctx,
        }
        out = approval_node(state)
        assert out == {"pending_approval": None, "approval_decision": None}
        # Must NOT create any ApprovalRequest under 'default' or anything else
        assert len(hitl._requests) == 0


@pytest.mark.asyncio
async def test_f4_02_graph_missing_thread_blocks_dangerous_but_allows_safe():
    """End-to-end graph without session_id blocks dangerous tools cleanly while leaving safe tools working."""
    reg = ToolRegistry()
    reg.register(execute_shell_command)
    reg.register(read_file)

    # Safe tool call without session_id succeeds
    safe_call = AIMessage(
        content="Reading file",
        tool_calls=[{"id": "call_safe_1", "name": "read_file", "args": {"file_path": "nope.txt"}}],
    )
    final_msg = AIMessage(content="Finished safe check.")
    graph_safe = build_agent_graph(
        model=_MockToolCallingChatModel(responses=[safe_call, final_msg]),
        registry=reg,
        checkpointer=MemorySaver(),
    )
    safe_res = await graph_safe.ainvoke(
        {"messages": [HumanMessage(content="read")], "run_context": {}},
        config={"configurable": {"thread_id": "ephemeral_safe"}},
    )
    safe_tool_msgs = [m for m in safe_res["messages"] if isinstance(m, ToolMessage)]
    assert len(safe_tool_msgs) == 1
    assert "Error: File does not exist" in safe_tool_msgs[0].content

    # Dangerous tool call without session_id in run_context fails closed (no interrupt, no execution)
    danger_call = AIMessage(
        content="Running dangerous command",
        tool_calls=[
            {
                "id": "call_danger_no_ctx",
                "name": "execute_shell_command",
                "args": {"command": "echo PWNED"},
            }
        ],
    )
    graph_danger = build_agent_graph(
        model=_MockToolCallingChatModel(responses=[danger_call, AIMessage(content="Blocked.")]),
        registry=reg,
        checkpointer=MemorySaver(),
    )
    danger_res = await graph_danger.ainvoke(
        {"messages": [HumanMessage(content="run")], "run_context": {}},
        config={"configurable": {"thread_id": "ephemeral_danger"}},
    )
    assert len(get_hitl_manager()._requests) == 0
    danger_tool_msgs = [m for m in danger_res["messages"] if isinstance(m, ToolMessage)]
    assert len(danger_tool_msgs) == 1
    assert "Operation cancelled" in danger_tool_msgs[0].content
    assert "PWNED" not in danger_tool_msgs[0].content


# -------------------------------------------------------------------------
# Fix 3: Approval API Thread Isolation & Trusted Server Session Binding
# -------------------------------------------------------------------------


def test_f3_01_api_list_and_get_request_thread_isolation(isolated_services):
    """GET /approvals and GET /approvals/{id} must enforce thread_id and avoid existence leaks."""
    test_ss, _test_as, _reg = isolated_services
    hitl = get_hitl_manager()

    test_ss.create_session(session_id="sess_api_A", title="Session A")
    test_ss.create_session(session_id="sess_api_B", title="Session B")

    req_a = hitl.create_request(
        thread_id="sess_api_A",
        run_id="run_a",
        tool_name="execute_shell_command",
        arguments={"command": "echo secret_A"},
        tool_call_id="call_api_a",
    )
    req_b = hitl.create_request(
        thread_id="sess_api_B",
        run_id="run_b",
        tool_name="execute_shell_command",
        arguments={"command": "echo secret_B"},
        tool_call_id="call_api_b",
    )

    app = create_app()
    with TestClient(app) as client:
        # 1. Unscoped GET /approvals without thread_id must NOT dump all threads
        r_no_tid = client.get("/api/v1/approvals")
        assert r_no_tid.status_code in (400, 404, 422)

        # 2. Scoped list for sess_api_A returns only req_a
        r_list_a = client.get("/api/v1/approvals?thread_id=sess_api_A")
        assert r_list_a.status_code == 200
        ids_a = [item["id"] for item in r_list_a.json()]
        assert ids_a == [req_a.id]

        # 3. GET /approvals/{approval_id} for own thread succeeds
        r_get_own = client.get(f"/api/v1/approvals/{req_a.id}?thread_id=sess_api_A")
        assert r_get_own.status_code == 200
        assert r_get_own.json()["id"] == req_a.id

        # 4. Cross-thread GET, non-existent ID, missing thread_id, and header mismatch
        #    MUST return identical 404 response without existence leakage
        r_cross = client.get(f"/api/v1/approvals/{req_a.id}?thread_id=sess_api_B")
        r_missing = client.get("/api/v1/approvals/appr_nonexistent?thread_id=sess_api_B")
        r_untracked = client.get(f"/api/v1/approvals/{req_a.id}?thread_id=unregistered_sess")
        r_header_conflict = client.get(
            f"/api/v1/approvals/{req_a.id}?thread_id=sess_api_A",
            headers={"X-Thread-ID": "sess_api_B"},
        )
        for resp in (r_cross, r_missing, r_untracked, r_header_conflict):
            assert resp.status_code == 404
            assert resp.json() == {"detail": "Approval request not found"}
        assert r_get_own.json()["id"] != req_b.id


@pytest.mark.asyncio
async def test_f3_02_api_decision_approve_reject_modify_thread_isolation(isolated_services):
    """POST /approvals/{id}/decision (approve/reject/modify + stream) must bind server session & thread."""
    test_ss, test_as, reg = isolated_services
    hitl = get_hitl_manager()

    # Build real interrupted flows on sess_A1, sess_A2, sess_A3 and a valid victim sess_B
    test_ss.create_session(session_id="sess_A1")
    test_ss.create_session(session_id="sess_A2")
    test_ss.create_session(session_id="sess_A3")
    test_ss.create_session(session_id="sess_B")

    # Trigger 3 concurrent interrupted sessions
    shared_cp = MemorySaver()
    responses_model = _MockToolCallingChatModel(
        responses=[
            AIMessage(
                content="Call 1",
                tool_calls=[
                    {
                        "id": "call_a1",
                        "name": "execute_shell_command",
                        "args": {"command": "echo api_approve"},
                    }
                ],
            ),
            AIMessage(
                content="Call 2",
                tool_calls=[
                    {
                        "id": "call_a2",
                        "name": "execute_shell_command",
                        "args": {"command": "echo api_reject"},
                    }
                ],
            ),
            AIMessage(
                content="Call 3",
                tool_calls=[
                    {
                        "id": "call_a3",
                        "name": "execute_shell_command",
                        "args": {"command": "echo original_cmd"},
                    }
                ],
            ),
            AIMessage(content="Approved finished"),
            AIMessage(content="Rejected finished"),
            AIMessage(content="Modified finished"),
        ]
    )
    test_as.graph = build_agent_graph(model=responses_model, registry=reg, checkpointer=shared_cp)

    # Trigger 3 independent interrupted threads
    r1 = await test_as.invoke_agent("sess_A1", "run 1")
    r2 = await test_as.invoke_agent("sess_A2", "run 2")
    r3 = await test_as.invoke_agent("sess_A3", "run 3")
    assert r1.status == "interrupted"
    assert r2.status == "interrupted"
    assert r3.status == "interrupted"

    appr_1 = r1.pending_approval["approval_id"]
    appr_2 = r2.pending_approval["approval_id"]
    appr_3 = r3.pending_approval["approval_id"]

    app = create_app()
    with TestClient(app) as client:
        # Cross-thread attack: sess_B tries to approve appr_1
        bad_cross = client.post(
            f"/api/v1/approvals/{appr_1}/decision",
            json={"thread_id": "sess_B", "decision": "approve"},
        )
        # Untrusted/nonexistent thread_id forged in body (even guessing appr_1)
        bad_forged = client.post(
            f"/api/v1/approvals/{appr_1}/decision",
            json={"thread_id": "forged_sess", "decision": "approve"},
        )
        # Caller header context X-Thread-ID: sess_B conflicts with body thread_id: sess_A1
        bad_header = client.post(
            f"/api/v1/approvals/{appr_1}/decision",
            headers={"X-Thread-ID": "sess_B"},
            json={"thread_id": "sess_A1", "decision": "approve"},
        )
        # Unknown approval_id must give the exact same 404 detail (no existence leak)
        bad_unknown = client.post(
            "/api/v1/approvals/appr_unknown/decision",
            json={"thread_id": "sess_B", "decision": "approve"},
        )
        # Streaming endpoint cross-thread check
        bad_stream = client.post(
            f"/api/v1/approvals/{appr_1}/decision/stream",
            json={"thread_id": "sess_B", "decision": "approve"},
        )
        for bad_resp in (bad_cross, bad_forged, bad_header, bad_unknown, bad_stream):
            assert bad_resp.status_code == 404
            assert bad_resp.json() == {"detail": "Approval request not found"}

        # appr_1 MUST remain cleanly PENDING
        assert hitl.get_request(appr_1, thread_id="sess_A1").status == ApprovalStatus.PENDING

        # Legitimate approve on sess_A1
        ok_approve = client.post(
            f"/api/v1/approvals/{appr_1}/decision",
            json={"thread_id": "sess_A1", "decision": "approve", "reason": "ok"},
        )
        assert ok_approve.status_code == 200
        assert ok_approve.json()["status"] == "completed"
        assert hitl.get_request(appr_1, thread_id="sess_A1").status == ApprovalStatus.CONSUMED

        # Legitimate reject on sess_A2
        ok_reject = client.post(
            f"/api/v1/approvals/{appr_2}/decision",
            json={"thread_id": "sess_A2", "decision": "reject", "reason": "unsafe"},
        )
        assert ok_reject.status_code == 200
        assert ok_reject.json()["status"] == "completed"
        assert hitl.get_request(appr_2, thread_id="sess_A2").status == ApprovalStatus.REJECTED

        # Legitimate modify on sess_A3
        ok_modify = client.post(
            f"/api/v1/approvals/{appr_3}/decision",
            json={
                "thread_id": "sess_A3",
                "decision": "modify",
                "modified_arguments": {"command": "echo modified_api_ok"},
            },
        )
        assert ok_modify.status_code == 200
        assert ok_modify.json()["status"] == "completed"
        assert hitl.get_request(appr_3, thread_id="sess_A3").status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_f3_03_api_stream_decision_with_matching_header_succeeds(isolated_services):
    """Streaming POST /approvals/{id}/decision/stream succeeds for verified thread + header."""
    test_ss, test_as, reg = isolated_services
    hitl = get_hitl_manager()
    test_ss.create_session(session_id="sess_stream_A")

    test_as.graph = build_agent_graph(
        model=_MockToolCallingChatModel(
            responses=[
                AIMessage(
                    content="Call stream",
                    tool_calls=[
                        {
                            "id": "call_st_1",
                            "name": "execute_shell_command",
                            "args": {"command": "echo stream_ok"},
                        }
                    ],
                ),
                AIMessage(content="Stream completed."),
            ]
        ),
        registry=reg,
        checkpointer=MemorySaver(),
    )

    res = await test_as.invoke_agent("sess_stream_A", "run stream")
    assert res.status == "interrupted"
    appr_id = res.pending_approval["approval_id"]

    app = create_app()
    with TestClient(app) as client:
        ok_stream = client.post(
            f"/api/v1/approvals/{appr_id}/decision/stream",
            headers={"X-Thread-ID": "sess_stream_A"},
            json={"thread_id": "sess_stream_A", "decision": "approve"},
        )
        assert ok_stream.status_code == 200
        assert "Stream completed." in ok_stream.text
        assert hitl.get_request(appr_id, thread_id="sess_stream_A").status == ApprovalStatus.CONSUMED


# -------------------------------------------------------------------------
# True Multi-Threaded Concurrency Tests (threading.Barrier)
# -------------------------------------------------------------------------


def test_mt_01_barrier_concurrent_lease_acquisition_single_winner():
    """8 OS threads racing through a threading.Barrier for the same lease -> exactly 1 wins."""
    hitl = get_hitl_manager()
    thread_id = "thread_mt_01"
    call_id = "call_mt_01"
    raw_args = {"command": "echo barrier_race"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_mt_01",
        tool_name="execute_shell_command",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    results: list[tuple[bool, object, str | None]] = []
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            outcome = hitl.acquire_execution_lease(
                approval_id=req.id,
                thread_id=thread_id,
                tool_call_id=call_id,
                arguments_hash=req.arguments_hash,
            )
            with results_lock:
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001, pragma: no cover
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(results) == worker_count
    winners = [r for r in results if r[0] is True]
    losers = [r for r in results if r[0] is False]
    assert len(winners) == 1
    assert len(losers) == worker_count - 1
    assert hitl.get_request(req.id).status == ApprovalStatus.EXECUTING


def test_mt_02_barrier_concurrent_real_executor_single_execution():
    """4 OS threads running independent asyncio loops racing the real executor -> tool runs ONCE."""
    reg = ToolRegistry()
    exec_counter = 0
    counter_lock = threading.Lock()

    @tool
    def atomic_counted_tool(tag: str) -> str:
        """Tool counting exact OS-level physical executions."""
        nonlocal exec_counter
        with counter_lock:
            exec_counter += 1
        return f"EXECUTED:{tag}"

    tag_tool_safety(atomic_counted_tool, risk_level=RiskLevel.HIGH, requires_approval=True)
    reg.register(atomic_counted_tool)

    hitl = get_hitl_manager()
    thread_id = "thread_mt_02"
    call_id = "call_mt_02"
    raw_args = {"tag": "once_only"}

    req = hitl.create_request(
        thread_id=thread_id,
        run_id="run_mt_02",
        tool_name="atomic_counted_tool",
        arguments=raw_args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(req.id, ApprovalDecision(decision="approve"), thread_id=thread_id)

    executor = create_tool_executor_node(reg)
    base_state = {
        "messages": [
            AIMessage(
                content="race",
                tool_calls=[{"id": call_id, "name": "atomic_counted_tool", "args": raw_args}],
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

    worker_count = 4
    barrier = threading.Barrier(worker_count)
    outputs: list[str] = []
    out_lock = threading.Lock()
    errors: list[BaseException] = []

    def run_executor_in_thread():
        try:
            barrier.wait(timeout=5)
            res = asyncio.run(executor(dict(base_state)))
            content = res["messages"][0].content
            with out_lock:
                outputs.append(content)
        except Exception as exc:  # noqa: BLE001, pragma: no cover
            with out_lock:
                errors.append(exc)

    threads = [threading.Thread(target=run_executor_in_thread) for _ in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(outputs) == worker_count
    assert exec_counter == 1
    assert sum(1 for o in outputs if "EXECUTED:once_only" in o) == 1
    assert sum(1 for o in outputs if "Operation cancelled" in o) == worker_count - 1
    assert hitl.get_request(req.id).status == ApprovalStatus.CONSUMED
