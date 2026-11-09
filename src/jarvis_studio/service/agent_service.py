"""High-level Agent Application Service decoupling API/UI from LangGraph."""

import time
from collections.abc import AsyncGenerator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import ApprovalDecision, get_hitl_manager
from jarvis_studio.core.guard.route_guard import GuardAction, get_route_guard
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.trace_logger import ConversationTrace, get_trace_store
from jarvis_studio.core.observability.tracer import set_trace_context, timed_step
from jarvis_studio.core.thinking.engine import ThinkingEngine
from jarvis_studio.service.session_service import get_session_service


class AgentExecutionResult:
    """Encapsulates the outcome of an agent invocation."""

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        status: str,  # "completed" | "interrupted" | "error"
        output_text: str = "",
        pending_approval: dict[str, Any] | None = None,
        error: str | None = None,
    ):
        self.thread_id = thread_id
        self.run_id = run_id
        self.status = status
        self.output_text = output_text
        self.pending_approval = pending_approval
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "status": self.status,
            "output_text": self.output_text,
            "pending_approval": self.pending_approval,
            "error": self.error,
        }


class AgentService:
    """Service orchestrating agent executions, checkpoints, and HITL resumptions."""

    def __init__(self, graph=None):
        self.graph = graph or build_agent_graph()
        self.session_service = get_session_service()
        self.hitl_manager = get_hitl_manager()

    def rebuild_graph(self, registry=None, model=None, checkpointer=None) -> None:
        """Rebuild and recompile the StateGraph with updated tools or configuration."""
        self.graph = build_agent_graph(
            model=model,
            registry=registry,
            checkpointer=checkpointer,
        )

    async def invoke_agent(
        self,
        thread_id: str,
        user_message: str,
        run_id: str | None = None,
    ) -> AgentExecutionResult:
        """Execute the agent on a specific thread with user input."""
        self.session_service.get_or_create_session(thread_id)
        effective_run_id = set_trace_context(thread_id, run_id)
        guard = get_route_guard()
        trace_store = get_trace_store()

        trace = ConversationTrace(
            session_id=thread_id,
            run_id=effective_run_id,
            query=user_message,
        )
        trace.input_stage = {"query": user_message, "thread_id": thread_id}

        # 1. Front-line Guard Evaluation (<50ms fast-path rule check)
        guard_start = time.time()
        decision = guard.evaluate(user_message, thread_id)
        guard_ms = int((time.time() - guard_start) * 1000)
        trace.guard_stage = {
            "action": decision.action.value,
            "rule": decision.matched_rule,
            "duration_ms": guard_ms,
        }

        # Handle Fast-Path Direct Reply or Emergency Halt
        if decision.action in (GuardAction.DIRECT_REPLY, GuardAction.HALT_TASK):
            reply_text = decision.reply_text or "已处理。"
            self.session_service.add_message(thread_id, role="user", content=user_message)
            self.session_service.add_message(thread_id, role="assistant", content=reply_text)
            trace.mark_ttft()
            trace.mark_ttfa()
            trace.finish(output=reply_text, status="rule_direct")
            trace_store.save_trace(trace)

            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="completed",
                output_text=reply_text,
            )

        # Record user message and handle enriched intent
        actual_input_message = decision.enriched_message or user_message
        self.session_service.add_message(thread_id, role="user", content=user_message)

        config = {"configurable": {"thread_id": thread_id}}
        
        # Check if state needs historical context from SQLite
        state_snapshot = await self.graph.aget_state(config)
        messages_input = [HumanMessage(content=actual_input_message)]
        if not state_snapshot.values.get("messages"):
            past_records = self.session_service.get_messages(thread_id)
            if len(past_records) > 1:
                history_msgs = []
                for rec in past_records[:-1]:
                    if rec.role == "user":
                        history_msgs.append(HumanMessage(content=rec.content))
                    elif rec.role == "assistant":
                        history_msgs.append(AIMessage(content=rec.content))
                messages_input = history_msgs + [HumanMessage(content=actual_input_message)]

        trace.preload_stage = {
            "history_loaded_count": len(messages_input) - 1,
            "sticky_context": guard.get_sticky_context(thread_id),
        }

        input_state = {
            "messages": messages_input,
            "run_context": {
                "session_id": thread_id,
                "run_id": effective_run_id,
            },
        }

        try:
            with timed_step("agent_run", thread_id=thread_id, run_id=effective_run_id):
                final_state = await self.graph.ainvoke(input_state, config=config)

            # Check if execution paused on an interrupt
            state_snapshot = await self.graph.aget_state(config)
            if state_snapshot.next:
                # Graph was interrupted (e.g. approval_gate)
                tasks = state_snapshot.tasks
                interrupt_val = None
                for task in tasks:
                    if task.interrupts:
                        interrupt_val = task.interrupts[0].value
                        break

                trace.finish(output="[Pending Approval]", status="interrupted")
                trace_store.save_trace(trace)

                return AgentExecutionResult(
                    thread_id=thread_id,
                    run_id=effective_run_id,
                    status="interrupted",
                    pending_approval=interrupt_val,
                )

            # Completed normally
            messages = final_state.get("messages", [])
            output = ""
            for m in reversed(messages):
                if isinstance(m, AIMessage) and m.content:
                    output = str(m.content)
                    break

            if output:
                self.session_service.add_message(thread_id, role="assistant", content=output)

            trace.finish(output=output, status="completed")
            trace_store.save_trace(trace)

            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="completed",
                output_text=output,
            )

        except Exception as e:
            logger.error(f"Error during agent invocation on thread {thread_id}: {e}")
            trace.finish(output="", error=str(e), status="error")
            trace_store.save_trace(trace)
            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="error",
                error=str(e),
            )

    async def stream_agent(
        self,
        thread_id: str,
        user_message: str,
        run_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream token-by-token and tool execution steps from the StateGraph."""
        self.session_service.get_or_create_session(thread_id)
        effective_run_id = set_trace_context(thread_id, run_id)
        guard = get_route_guard()
        trace_store = get_trace_store()

        trace = ConversationTrace(
            session_id=thread_id,
            run_id=effective_run_id,
            query=user_message,
        )
        trace.input_stage = {"query": user_message, "thread_id": thread_id}

        # 1. Front-line Guard Evaluation (<50ms fast-path rule check)
        guard_start = time.time()
        decision = guard.evaluate(user_message, thread_id)
        guard_ms = int((time.time() - guard_start) * 1000)
        trace.guard_stage = {
            "action": decision.action.value,
            "rule": decision.matched_rule,
            "duration_ms": guard_ms,
        }

        # Fast-Path Rule Direct Reply (0 tokens, <50ms return)
        if decision.action in (GuardAction.DIRECT_REPLY, GuardAction.HALT_TASK):
            reply_text = decision.reply_text or "已处理。"
            self.session_service.add_message(thread_id, role="user", content=user_message)
            self.session_service.add_message(thread_id, role="assistant", content=reply_text)
            trace.mark_ttft()
            trace.mark_ttfa()
            trace.finish(output=reply_text, status="rule_direct")
            trace_store.save_trace(trace)

            yield {
                "event": "thinking",
                "data": {
                    "phase": "understand",
                    "status": "completed",
                    "text": "命中规则守卫直出通道 (<50ms)",
                    "elapsed_ms": guard_ms,
                },
            }
            yield {"event": "token", "data": {"token": reply_text}}
            yield {
                "event": "completed",
                "data": {
                    "thread_id": thread_id,
                    "run_id": effective_run_id,
                    "output_text": reply_text,
                    "trace_id": trace.trace_id,
                    "fast_path": True,
                },
            }
            return

        # Regular StateGraph execution
        actual_input_message = decision.enriched_message or user_message
        self.session_service.add_message(thread_id, role="user", content=user_message)
        config = {"configurable": {"thread_id": thread_id}}

        # Check if state needs historical context from SQLite
        state_snapshot = await self.graph.aget_state(config)
        messages_input = [HumanMessage(content=actual_input_message)]
        if not state_snapshot.values.get("messages"):
            past_records = self.session_service.get_messages(thread_id)
            if len(past_records) > 1:
                history_msgs = []
                for rec in past_records[:-1]:
                    if rec.role == "user":
                        history_msgs.append(HumanMessage(content=rec.content))
                    elif rec.role == "assistant":
                        history_msgs.append(AIMessage(content=rec.content))
                messages_input = history_msgs + [HumanMessage(content=actual_input_message)]

        trace.preload_stage = {
            "history_loaded_count": len(messages_input) - 1,
            "sticky_context": guard.get_sticky_context(thread_id),
        }

        input_state = {
            "messages": messages_input,
            "run_context": {
                "session_id": thread_id,
                "run_id": effective_run_id,
            },
        }

        thinking_engine = ThinkingEngine(thread_id)
        # Phase 1: Emit immediate understanding event (<100ms)
        await thinking_engine.emit_understand("正在理解您的指令并分析上下文...")
        trace.mark_ttft()

        for ev in await thinking_engine.get_queued_events():
            yield ev

        active_tools: dict[str, tuple[float, Any]] = {}
        trace.add_step(
            step_type="understand",
            name="意图分析与上下文检查",
            arguments={"query": user_message},
            result={"action": decision.action.value, "rule_name": decision.matched_rule},
            duration_ms=guard_ms,
        )

        try:
            async for event in self.graph.astream_events(
                input_state, config=config, version="v2"
            ):
                # Drain any heartbeat or thinking events
                for ev in await thinking_engine.get_queued_events():
                    yield ev

                kind = event.get("event")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        trace.mark_ttfa()
                        thinking_engine.start_heartbeat()
                        yield {"event": "token", "data": {"token": str(chunk.content)}}

                elif kind == "on_tool_start":
                    t_name = str(event.get("name") or "tool")
                    t_input = event.get("data", {}).get("input")
                    active_tools[t_name] = (time.time(), t_input)
                    await thinking_engine.emit_tool_start(t_name, t_input)
                    for ev in await thinking_engine.get_queued_events():
                        yield ev
                    yield {
                        "event": "tool_start",
                        "data": {
                            "tool": t_name,
                            "input": t_input,
                        },
                    }

                elif kind == "on_tool_end":
                    t_name = str(event.get("name") or "tool")
                    raw_out = event.get("data", {}).get("output")
                    out_str = str(raw_out)[:2000] if raw_out is not None else ""
                    start_t, t_args = active_tools.pop(t_name, (time.time(), None))
                    t_duration_ms = int((time.time() - start_t) * 1000)

                    trace.add_step(
                        step_type="tool",
                        name=t_name,
                        arguments=t_args,
                        result=out_str[:500],
                        duration_ms=t_duration_ms,
                    )
                    await thinking_engine.emit_tool_end(t_name, out_str)
                    for ev in await thinking_engine.get_queued_events():
                        yield ev
                    yield {
                        "event": "tool_end",
                        "data": {
                            "tool": t_name,
                            "output": out_str,
                            "duration_ms": t_duration_ms,
                        },
                    }

            # Phase 4: Finalize thinking
            trace.add_step(
                step_type="organize",
                name="组织输出并交付",
                duration_ms=0,
            )
            await thinking_engine.emit_completed()
            for ev in await thinking_engine.get_queued_events():
                yield ev
            thinking_engine.close()

            state_snapshot = await self.graph.aget_state(config)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for task in tasks:
                    if task.interrupts:
                        interrupt_val = task.interrupts[0].value
                        break
                trace.finish(output="[Pending Approval]", status="interrupted")
                trace_store.save_trace(trace)
                yield {
                    "event": "approval_required",
                    "data": interrupt_val or {"status": "pending_approval"},
                }
            else:
                messages = state_snapshot.values.get("messages", [])
                output = ""
                for m in reversed(messages):
                    if isinstance(m, AIMessage) and m.content:
                        output = str(m.content)
                        break
                if output:
                    self.session_service.add_message(thread_id, role="assistant", content=output)

                trace.finish(output=output, status="completed")
                trace_store.save_trace(trace)

                yield {
                    "event": "completed",
                    "data": {
                        "thread_id": thread_id,
                        "run_id": effective_run_id,
                        "output_text": output,
                        "trace_id": trace.trace_id,
                        "ttft_ms": trace.ttft_ms,
                        "ttfa_ms": trace.ttfa_ms,
                        "ttlt_ms": trace.ttlt_ms,
                    },
                }

        except Exception as e:
            thinking_engine.close()
            logger.error(f"Error during streaming execution on thread {thread_id}: {e}")
            trace.finish(output="", error=str(e), status="error")
            trace_store.save_trace(trace)
            yield {"event": "error", "data": {"error": str(e), "trace_id": trace.trace_id}}

    async def resume_approval(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AgentExecutionResult:
        """Resume an interrupted agent run by submitting an approval decision."""
        effective_run_id = set_trace_context(thread_id)
        config = {"configurable": {"thread_id": thread_id}}

        logger.info(
            f"Resuming interrupted execution on thread '{thread_id}' with decision '{decision.decision}'"
        )

        try:
            resume_payload = {
                "decision": decision.decision,
                "reason": decision.reason,
                "modified_arguments": decision.modified_arguments,
            }
            final_state = await self.graph.ainvoke(
                Command(resume=resume_payload),
                config=config,
            )

            state_snapshot = await self.graph.aget_state(config)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for task in tasks:
                    if task.interrupts:
                        interrupt_val = task.interrupts[0].value
                        break
                return AgentExecutionResult(
                    thread_id=thread_id,
                    run_id=effective_run_id,
                    status="interrupted",
                    pending_approval=interrupt_val,
                )

            messages = final_state.get("messages", [])
            output = ""
            for m in reversed(messages):
                if isinstance(m, AIMessage) and m.content:
                    output = str(m.content)
                    break

            if output:
                self.session_service.add_message(thread_id, role="assistant", content=output)

            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="completed",
                output_text=output,
            )
        except Exception as e:
            logger.error(f"Failed to resume thread {thread_id}: {e}")
            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="error",
                error=str(e),
            )

    async def stream_resume_approval(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Resume an interrupted agent run with real-time SSE event streaming."""
        effective_run_id = set_trace_context(thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        resume_payload = {
            "decision": decision.decision,
            "reason": decision.reason,
            "modified_arguments": decision.modified_arguments,
        }

        try:
            async for event in self.graph.astream_events(
                Command(resume=resume_payload), config=config, version="v2"
            ):
                kind = event.get("event")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield {"event": "token", "data": {"token": str(chunk.content)}}
                elif kind == "on_tool_start":
                    yield {
                        "event": "tool_start",
                        "data": {
                            "tool": event.get("name"),
                            "input": event.get("data", {}).get("input"),
                        },
                    }
                elif kind == "on_tool_end":
                    raw_out = event.get("data", {}).get("output")
                    out_str = str(raw_out)[:2000] if raw_out is not None else ""
                    yield {
                        "event": "tool_end",
                        "data": {
                            "tool": event.get("name"),
                            "output": out_str,
                        },
                    }

            state_snapshot = await self.graph.aget_state(config)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for task in tasks:
                    if task.interrupts:
                        interrupt_val = task.interrupts[0].value
                        break
                yield {
                    "event": "approval_required",
                    "data": interrupt_val or {"status": "pending_approval"},
                }
            else:
                messages = state_snapshot.values.get("messages", [])
                output = ""
                for m in reversed(messages):
                    if isinstance(m, AIMessage) and m.content:
                        output = str(m.content)
                        break
                if output:
                    self.session_service.add_message(thread_id, role="assistant", content=output)
                yield {
                    "event": "completed",
                    "data": {
                        "thread_id": thread_id,
                        "run_id": effective_run_id,
                        "output_text": output,
                    },
                }
        except Exception as e:
            logger.error(f"Error during streaming resume on thread {thread_id}: {e}")
            yield {"event": "error", "data": {"error": str(e)}}


_global_agent_service: AgentService | None = None


def get_agent_service() -> AgentService:
    """Retrieve the singleton AgentService instance."""
    global _global_agent_service
    if _global_agent_service is None:
        _global_agent_service = AgentService()
    return _global_agent_service
