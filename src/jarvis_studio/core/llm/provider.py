"""LLM Provider specifications and adapters."""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from jarvis_studio.config.models import LLMProviderType


class ProviderSpec(BaseModel):
    """Specification describing an LLM provider and its default capabilities."""
    provider_type: LLMProviderType
    default_model: str
    supports_streaming: bool = True
    supports_tool_calling: bool = True
    base_url_required: bool = False
    description: str = ""
    extra_headers: Dict[str, str] = Field(default_factory=dict)


PROVIDER_REGISTRY: Dict[LLMProviderType, ProviderSpec] = {
    LLMProviderType.OPENAI: ProviderSpec(
        provider_type=LLMProviderType.OPENAI,
        default_model="gpt-4o-mini",
        description="Official OpenAI API",
    ),
    LLMProviderType.DEEPSEEK: ProviderSpec(
        provider_type=LLMProviderType.DEEPSEEK,
        default_model="deepseek-chat",
        base_url_required=True,
        description="DeepSeek OpenAI-compatible API",
    ),
    LLMProviderType.ANTHROPIC: ProviderSpec(
        provider_type=LLMProviderType.ANTHROPIC,
        default_model="claude-3-5-sonnet-20240620",
        description="Anthropic Claude API",
    ),
    LLMProviderType.GEMINI: ProviderSpec(
        provider_type=LLMProviderType.GEMINI,
        default_model="gemini-1.5-pro",
        description="Google Generative AI Gemini",
    ),
    LLMProviderType.OLLAMA: ProviderSpec(
        provider_type=LLMProviderType.OLLAMA,
        default_model="llama3.1:8b",
        base_url_required=True,
        description="Local Ollama instance via OpenAI-compatible endpoint",
    ),
    LLMProviderType.CUSTOM: ProviderSpec(
        provider_type=LLMProviderType.CUSTOM,
        default_model="custom-model",
        base_url_required=True,
        description="Custom OpenAI-compatible inference server (vLLM, SGLang, etc.)",
    ),
}
