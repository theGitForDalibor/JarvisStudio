"""Assistant reasoning node for LangGraph."""

from typing import Any, Dict, List
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage

from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.tracer import timed_step

_DEFAULT_SYSTEM_PROMPT = """You are Jarvis, a highly capable personal AI Operating Assistant.
Your purpose is to assist the user with software engineering, file management, information retrieval, and system workflows.
You have access to a suite of tools. Use tools when needed to inspect, execute, or verify information.
For high-risk actions (e.g. destructive file edits, running shell commands), you must be clear and transparent.
Always prioritize correctness, security, and succinct, actionable communication."""


def create_assistant_node(model: BaseChatModel, system_prompt: str = _DEFAULT_SYSTEM_PROMPT):
    """Factory creating the assistant reasoning node bound with LLM and tools."""

    async def assistant_node(state: AgentState) -> Dict[str, Any]:
        """Invoke the chat model with conversation history and bound tools."""
        messages = list(state.get("messages", []))
        
        # Prepend system prompt if not already present
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_prompt)] + messages

        run_context = state.get("run_context", {})
        session_id = run_context.get("session_id", "default")
        run_id = run_context.get("run_id", "run_unknown")

        with timed_step("assistant_reasoning", session_id=session_id, run_id=run_id):
            response = await model.ainvoke(messages)

        logger.debug(
            f"Assistant generated message: {response.content[:100]}... [Tool calls: {len(response.tool_calls)}]"
        )

        return {
            "messages": [response],
        }

    return assistant_node
