"""Assistant reasoning node for LangGraph."""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage

from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.tracer import timed_step
from jarvis_studio.memory.manager import get_memory_manager

_DEFAULT_SYSTEM_PROMPT = """You are Jarvis, a highly capable personal AI Operating Assistant in AgentStudio.
Your purpose is to assist the user with software engineering, research & analysis, file management, information retrieval, and system workflows.
You have access to a suite of tools. Use tools when needed to inspect, execute, or verify information.
For high-risk actions (e.g. destructive file edits, running shell commands), you must be clear and transparent.

When handling Research & Analysis tasks:
1. Decompose complex research goals into focused sub-queries and run multiple `search_web` calls if needed.
2. Use `fetch_web_page` to inspect primary documentation and authoritative articles rather than relying solely on short snippets.
3. Explicitly distinguish Verified Facts (with real source URLs), Analytical Inferences, and Opinions/Assessments. State any information gaps or uncertainties honestly.
4. NEVER fabricate search results, statistics, or URLs. Only cite URLs actually observed from tools.
5. When a `task_id` is present in your Execution Context and the user requests a research report or deliverable, call `publish_research_report` with that exact `task_id` (and `run_id` if provided) to persist the final Markdown report as a durable Task Deliverable.

Always prioritize correctness, security, and succinct, actionable communication."""


def create_assistant_node(
    model: BaseChatModel, system_prompt: str = _DEFAULT_SYSTEM_PROMPT
):
    """Factory creating the assistant reasoning node bound with LLM and tools."""

    async def assistant_node(state: AgentState) -> dict[str, Any]:
        """Invoke the chat model with conversation history, injected LTM memory, and bound tools."""
        messages = list(state.get("messages", []))

        run_context = state.get("run_context") or {}
        session_id = run_context.get("session_id", "default")
        run_id = run_context.get("run_id", "run_unknown")
        task_id = run_context.get("task_id")

        # Dynamically append long-term memory preferences and active task context
        memory_mgr = get_memory_manager()
        memory_context = memory_mgr.get_formatted_context()
        effective_system_prompt = system_prompt
        if task_id:
            effective_system_prompt = (
                f"{effective_system_prompt}\n\n"
                f"[Execution Context]\n"
                f"- task_id: {task_id}\n"
                f"- run_id: {run_id}"
            )
        if memory_context:
            effective_system_prompt = f"{effective_system_prompt}\n{memory_context}"

        # Prepend system prompt if not already present
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=effective_system_prompt)] + messages

        with timed_step("assistant_reasoning", session_id=session_id, run_id=run_id):
            response = await model.ainvoke(messages)

        logger.debug(
            f"Assistant generated message: {response.content[:100]}... [Tool calls: {len(response.tool_calls)}]"
        )

        return {
            "messages": [response],
        }

    return assistant_node
