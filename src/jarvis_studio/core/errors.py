"""Domain exception hierarchy for Jarvis Studio."""

from typing import Any, Dict, Optional


class JarvisError(Exception):
    """Base exception for all Jarvis Studio errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigurationError(JarvisError):
    """Raised when configuration is invalid or missing required credentials."""
    pass


class LLMProviderError(JarvisError):
    """Raised when an LLM provider fails to initialize or generate."""
    pass


class ToolExecutionError(JarvisError):
    """Raised when a tool execution fails."""
    pass


class ApprovalRequiredError(JarvisError):
    """Raised when an action is blocked pending human confirmation."""

    def __init__(self, tool_name: str, arguments: Dict[str, Any], approval_id: str):
        super().__init__(
            f"Action '{tool_name}' requires human approval before execution.",
            details={
                "tool_name": tool_name,
                "arguments": arguments,
                "approval_id": approval_id,
            },
        )
        self.tool_name = tool_name
        self.arguments = arguments
        self.approval_id = approval_id


class StateCheckpointError(JarvisError):
    """Raised when agent state checkpointing or restoration fails."""
    pass
