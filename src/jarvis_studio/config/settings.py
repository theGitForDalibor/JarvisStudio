"""Central settings management for Jarvis Studio."""

import os
from pathlib import Path
from typing import Optional
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from jarvis_studio.config.models import (
    AppConfig,
    LLMConfig,
    ObservabilityConfig,
    SecurityConfig,
    StorageConfig,
)


class Settings(BaseSettings):
    """Global unified settings for Jarvis Studio."""

    app: AppConfig = AppConfig()
    llm: LLMConfig = LLMConfig()
    security: SecurityConfig = SecurityConfig()
    storage: StorageConfig = StorageConfig()
    observability: ObservabilityConfig = ObservabilityConfig()

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def load_from_env_and_yaml(cls, yaml_path: Optional[str] = "config/settings.yaml") -> "Settings":
        """Load settings from environment variables with optional YAML overrides."""
        settings_kwargs = {}
        if yaml_path and os.path.exists(yaml_path):
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f)
                    if isinstance(yaml_data, dict):
                        settings_kwargs = yaml_data
            except Exception as e:
                # Log error or fallback silently
                pass

        instance = cls(**settings_kwargs)
        
        # Override flat env variables if present
        if openai_key := os.getenv("OPENAI_API_KEY"):
            instance.llm.openai_api_key = openai_key
        if deepseek_key := os.getenv("DEEPSEEK_API_KEY"):
            instance.llm.deepseek_api_key = deepseek_key
        if google_key := os.getenv("GOOGLE_API_KEY"):
            instance.llm.google_api_key = google_key
        if anthropic_key := os.getenv("ANTHROPIC_API_KEY"):
            instance.llm.anthropic_api_key = anthropic_key
        if default_provider := os.getenv("DEFAULT_LLM_PROVIDER"):
            instance.llm.default_provider = default_provider
        if default_model := os.getenv("DEFAULT_MODEL_NAME"):
            instance.llm.default_model = default_model
        if sqlite_path := os.getenv("SQLITE_DB_PATH"):
            instance.storage.sqlite_path = sqlite_path
        if checkpoint_backend := os.getenv("CHECKPOINT_BACKEND"):
            instance.storage.checkpoint_backend = checkpoint_backend

        # Ensure directories exist
        Path(instance.storage.workspace_root).mkdir(parents=True, exist_ok=True)
        if instance.storage.checkpoint_backend == "sqlite":
            Path(instance.storage.sqlite_path).parent.mkdir(parents=True, exist_ok=True)

        return instance


_global_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Retrieve the cached global settings instance."""
    global _global_settings
    if _global_settings is None:
        _global_settings = Settings.load_from_env_and_yaml()
    return _global_settings
