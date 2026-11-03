"""Domain exception hierarchy for Jarvis Studio."""

from typing import Any


class JarvisError(Exception):
    """Base exception for all Jarvis Studio errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigurationError(JarvisError):
    """Raised when configuration is invalid or missing required credentials."""


class LLMProviderError(JarvisError):
    """Raised when an LLM provider fails to initialize or generate."""


class ToolExecutionError(JarvisError):
    """Raised when a tool execution fails."""


class ApprovalRequiredError(JarvisError):
    """Raised when an action is blocked pending human confirmation."""

    def __init__(self, tool_name: str, arguments: dict[str, Any], approval_id: str):
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
