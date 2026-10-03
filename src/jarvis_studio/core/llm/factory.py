"""Unified Factory to instantiate LangChain ChatModels across swappable providers."""

from typing import Any, Optional
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from jarvis_studio.config.models import LLMConfig, LLMProviderType
from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.errors import ConfigurationError, LLMProviderError
from jarvis_studio.core.observability.logger import logger


class LLMFactory:
    """Factory creating configured ChatModel instances."""

    @classmethod
    def create_model(
        cls,
        provider: Optional[LLMProviderType] = None,
        model_name: Optional[str] = None,
        temperature: Optional[float] = None,
        streaming: Optional[bool] = None,
        config: Optional[LLMConfig] = None,
    ) -> BaseChatModel:
        """Create a BaseChatModel instance based on parameters and application settings."""
        cfg = config or get_settings().llm
        provider_type = provider or cfg.default_provider
        temp = temperature if temperature is not None else cfg.temperature
        stream = streaming if streaming is not None else cfg.streaming

        logger.info(f"Instantiating ChatModel for provider: '{provider_type}'")

        match provider_type:
            case LLMProviderType.OPENAI:
                return cls._create_openai_model(
                    model_name=model_name or cfg.default_model,
                    api_key=cfg.openai_api_key,
                    base_url=cfg.openai_base_url,
                    temperature=temp,
                    streaming=stream,
                )

            case LLMProviderType.DEEPSEEK:
                return cls._create_openai_compatible_model(
                    model_name=model_name or cfg.deepseek_model_name,
                    api_key=cfg.deepseek_api_key,
                    base_url=cfg.deepseek_base_url or "https://api.deepseek.com/v1",
                    temperature=temp,
                    streaming=stream,
                )

            case LLMProviderType.OLLAMA:
                return cls._create_openai_compatible_model(
                    model_name=model_name or cfg.ollama_model_name,
                    api_key="ollama",  # Ollama does not require a key but client requires non-empty
                    base_url=cfg.ollama_base_url,
                    temperature=temp,
                    streaming=stream,
                )

            case LLMProviderType.CUSTOM:
                return cls._create_openai_compatible_model(
                    model_name=model_name or cfg.default_model,
                    api_key=cfg.openai_api_key or "custom-key",
                    base_url=cfg.openai_base_url,
                    temperature=temp,
                    streaming=stream,
                )

            case LLMProviderType.ANTHROPIC:
                return cls._create_anthropic_model(
                    model_name=model_name or cfg.anthropic_model_name,
                    api_key=cfg.anthropic_api_key,
                    temperature=temp,
                    streaming=stream,
                )

            case LLMProviderType.GEMINI:
                return cls._create_gemini_model(
                    model_name=model_name or cfg.gemini_model_name,
                    api_key=cfg.google_api_key,
                    temperature=temp,
                    streaming=stream,
                )

            case _:
                raise LLMProviderError(f"Unsupported LLM provider: {provider_type}")

    @staticmethod
    def _create_openai_model(
        model_name: str,
        api_key: Optional[str],
        base_url: Optional[str],
        temperature: float,
        streaming: bool,
    ) -> BaseChatModel:
        # If API key is not set, ChatOpenAI will check OPENAI_API_KEY env var
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            streaming=streaming,
        )

    @staticmethod
    def _create_openai_compatible_model(
        model_name: str,
        api_key: Optional[str],
        base_url: Optional[str],
        temperature: float,
        streaming: bool,
    ) -> BaseChatModel:
        if not base_url:
            raise ConfigurationError("base_url is required for OpenAI-compatible providers")
        return ChatOpenAI(
            model=model_name,
            api_key=api_key or "placeholder",
            base_url=base_url,
            temperature=temperature,
            streaming=streaming,
        )

    @staticmethod
    def _create_anthropic_model(
        model_name: str,
        api_key: Optional[str],
        temperature: float,
        streaming: bool,
    ) -> BaseChatModel:
        try:
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(
                model=model_name,
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
            )
        except ImportError:
            raise ConfigurationError(
                "langchain-anthropic is not installed. Install with `uv add langchain-anthropic`"
            )

    @staticmethod
    def _create_gemini_model(
        model_name: str,
        api_key: Optional[str],
        temperature: float,
        streaming: bool,
    ) -> BaseChatModel:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(
                model=model_name,
                google_api_key=api_key,
                temperature=temperature,
                streaming=streaming,
            )
        except ImportError:
            raise ConfigurationError(
                "langchain-google-genai is not installed. Install with `uv add langchain-google-genai`"
            )
