"""Configuration data models for Jarvis Studio."""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class LLMProviderType(str, Enum):
    """Supported LLM Provider Types."""
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    CUSTOM = "custom"


class LLMConfig(BaseModel):
    """Configuration for LLM generation and adapters."""
    default_provider: LLMProviderType = Field(
        default=LLMProviderType.OPENAI,
        description="Default provider to route queries to",
    )
    default_model: str = Field(
        default="gpt-4o-mini",
        description="Default model name for the provider",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    streaming: bool = Field(default=True)
    max_tokens: Optional[int] = Field(default=None)

    # Provider specific keys and endpoints
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = "https://api.openai.com/v1"

    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = "https://api.deepseek.com/v1"
    deepseek_model_name: str = "deepseek-chat"

    anthropic_api_key: Optional[str] = None
    anthropic_model_name: str = "claude-3-5-sonnet-20240620"

    google_api_key: Optional[str] = None
    gemini_model_name: str = "gemini-1.5-pro"

    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model_name: str = "llama3.1:8b"


class SecurityConfig(BaseModel):
    """Security and Human-In-The-Loop (HITL) policies."""
    hitl_enabled: bool = Field(
        default=True,
        description="Whether high-risk tools require explicit human approval",
    )
    auto_approve_read_only: bool = Field(
        default=True,
        description="Automatically approve safe read-only operations",
    )
    dangerous_tools: List[str] = Field(
        default_factory=lambda: [
            "execute_shell_command",
            "write_file",
            "delete_file",
            "git_push",
        ],
        description="List of tool names that strictly require human confirmation",
    )


class StorageConfig(BaseModel):
    """Storage and Checkpointer configurations."""
    checkpoint_backend: str = Field(
        default="sqlite",
        description="Checkpointer backend: 'sqlite' or 'memory'",
    )
    sqlite_path: str = Field(default="data/jarvis_studio.db")
    workspace_root: str = Field(default="./data/workspace")


class ObservabilityConfig(BaseModel):
    """Observability and tracing options."""
    log_level: str = Field(default="INFO")
    enable_rich_logging: bool = Field(default=True)
    langsmith_tracing: bool = Field(default=False)
    langsmith_endpoint: Optional[str] = "https://api.smith.langchain.com"
    langsmith_api_key: Optional[str] = None
    langsmith_project: str = "jarvis-studio"


class AppConfig(BaseModel):
    """Application metadata."""
    name: str = "Jarvis Studio"
    version: str = "0.1.0"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True
