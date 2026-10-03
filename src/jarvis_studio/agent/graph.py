"""LangGraph Agent StateGraph construction and compilation."""

from typing import List, Optional
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from jarvis_studio.agent.checkpoints.saver import get_checkpointer
from jarvis_studio.agent.nodes.approval import create_approval_node
from jarvis_studio.agent.nodes.assistant import create_assistant_node
from jarvis_studio.agent.nodes.executor import create_tool_executor_node
from jarvis_studio.agent.nodes.router import route_after_assistant
from jarvis_studio.agent.state import AgentState
from jarvis_studio.core.llm.factory import LLMFactory
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def build_agent_graph(
    model: Optional[BaseChatModel] = None,
    registry: Optional[ToolRegistry] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    system_prompt: Optional[str] = None,
):
    """Construct, link, and compile the production LangGraph StateGraph."""
    tool_registry = registry or get_default_registry()
    tools = tool_registry.list_tools()

    # Create model if not passed
    chat_model = model or LLMFactory.create_model()

    # Bind tools to the model if tools exist
    bound_model = chat_model.bind_tools(tools) if tools else chat_model

    # Checkpointer
    saver = checkpointer or get_checkpointer()

    logger.info(
        f"Building StateGraph with {len(tools)} registered tool(s), checkpointer={type(saver).__name__}"
    )

    # Initialize StateGraph
    workflow = StateGraph(AgentState)

    # Add Nodes
    workflow.add_node(
        "assistant",
        create_assistant_node(
            bound_model,
            system_prompt=system_prompt or create_assistant_node.__defaults__[0]
            if create_assistant_node.__defaults__
            else "You are Jarvis Studio personal assistant.",
        ),
    )
    workflow.add_node("approval_gate", create_approval_node(tool_registry))
    workflow.add_node("tools", create_tool_executor_node(tool_registry))

    # Edges
    workflow.add_edge(START, "assistant")

    workflow.add_conditional_edges(
        "assistant",
        lambda state: route_after_assistant(state, tool_registry),
        {
            "approval_gate": "approval_gate",
            "tools": "tools",
            END: END,
        },
    )

    workflow.add_edge("approval_gate", "tools")
    workflow.add_edge("tools", "assistant")

    # Compile with persistent checkpoint saver
    compiled_graph = workflow.compile(checkpointer=saver)
    return compiled_graph
