"""High-level Agent Application Service decoupling API/UI from LangGraph."""

import uuid
from typing import Any, AsyncGenerator, Dict, Optional
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import ApprovalDecision, get_hitl_manager
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.tracer import set_trace_context, timed_step
from jarvis_studio.service.session_service import get_session_service


class AgentExecutionResult:
    """Encapsulates the outcome of an agent invocation."""

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        status: str,  # "completed" | "interrupted" | "error"
        output_text: str = "",
        pending_approval: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        self.thread_id = thread_id
        self.run_id = run_id
        self.status = status
        self.output_text = output_text
        self.pending_approval = pending_approval
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
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

    async def invoke_agent(
        self,
        thread_id: str,
        user_message: str,
        run_id: Optional[str] = None,
    ) -> AgentExecutionResult:
        """Execute the agent on a specific thread with user input."""
        session = self.session_service.get_or_create_session(thread_id)
        effective_run_id = set_trace_context(thread_id, run_id)

        config = {"configurable": {"thread_id": thread_id}}
        input_state = {
            "messages": [HumanMessage(content=user_message)],
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

            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="completed",
                output_text=output,
            )

        except Exception as e:
            logger.error(f"Error during agent invocation on thread {thread_id}: {e}")
            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                status="error",
                error=str(e),
            )

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
            # Resume graph using Command(resume=decision_dict)
            resume_payload = {
                "decision": decision.decision,
                "reason": decision.reason,
                "modified_arguments": decision.modified_arguments,
            }
            final_state = await self.graph.ainvoke(
                Command(resume=resume_payload),
                config=config,
            )

            # Check if another interrupt followed
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


_global_agent_service: Optional[AgentService] = None


def get_agent_service() -> AgentService:
    """Retrieve the singleton AgentService instance."""
    global _global_agent_service
    if _global_agent_service is None:
        _global_agent_service = AgentService()
    return _global_agent_service
