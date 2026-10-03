"""Unit tests for configuration loading and validation."""

from jarvis_studio.config.models import LLMProviderType
from jarvis_studio.config.settings import Settings, get_settings


def test_default_settings():
    """Verify default settings instantiation and schema validation."""
    settings = get_settings()
    assert settings.app.name == "Jarvis Studio"
    assert settings.app.version == "0.1.0"
    assert settings.security.hitl_enabled is True
    assert "execute_shell_command" in settings.security.dangerous_tools


def test_custom_settings_override():
    """Verify settings allow overriding values."""
    custom = Settings(
        app={"name": "Custom Jarvis", "port": 9000},
        llm={"default_provider": LLMProviderType.DEEPSEEK, "default_model": "deepseek-coder"},
    )
    assert custom.app.name == "Custom Jarvis"
    assert custom.app.port == 9000
    assert custom.llm.default_provider == LLMProviderType.DEEPSEEK
    assert custom.llm.default_model == "deepseek-coder"
