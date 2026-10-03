"""Unit tests for the LLM Factory."""

import pytest
from langchain_openai import ChatOpenAI
from jarvis_studio.config.models import LLMConfig, LLMProviderType
from jarvis_studio.core.llm.factory import LLMFactory


def test_create_openai_model():
    """Verify OpenAI chat model creation."""
    model = LLMFactory.create_model(
        provider=LLMProviderType.OPENAI,
        model_name="gpt-4o-mini",
        config=LLMConfig(openai_api_key="test-key"),
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-4o-mini"


def test_create_deepseek_model():
    """Verify DeepSeek OpenAI-compatible model creation."""
    model = LLMFactory.create_model(
        provider=LLMProviderType.DEEPSEEK,
        model_name="deepseek-chat",
        config=LLMConfig(
            deepseek_api_key="ds-test-key",
            deepseek_base_url="https://api.deepseek.com/v1",
        ),
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-chat"
    assert "deepseek.com" in str(model.openai_api_base)


def test_create_ollama_model():
    """Verify Ollama local endpoint adapter."""
    model = LLMFactory.create_model(
        provider=LLMProviderType.OLLAMA,
        model_name="llama3.1:8b",
        config=LLMConfig(ollama_base_url="http://localhost:11434/v1"),
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "llama3.1:8b"
