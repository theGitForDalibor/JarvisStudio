"""Central settings management for Jarvis Studio."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

from jarvis_studio.config.models import (
    AppConfig,
    LLMConfig,
    LLMProviderType,
    ObservabilityConfig,
    RAGConfig,
    SecurityConfig,
    StorageConfig,
)

# Load .env into os.environ before settings initialization
load_dotenv(override=False)


class Settings(BaseSettings):
    """Global unified settings for Jarvis Studio."""

    app: AppConfig = AppConfig()
    llm: LLMConfig = LLMConfig()
    security: SecurityConfig = SecurityConfig()
    storage: StorageConfig = StorageConfig()
    rag: RAGConfig = RAGConfig()
    observability: ObservabilityConfig = ObservabilityConfig()

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def load_from_env_and_yaml(
        cls, yaml_path: str | None = "config/settings.yaml"
    ) -> "Settings":
        """Load settings from environment variables with optional YAML overrides."""
        settings_kwargs = {}
        if yaml_path and os.path.exists(yaml_path):
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f)
                    if isinstance(yaml_data, dict):
                        settings_kwargs = yaml_data
            except Exception:
                pass

        instance = cls(**settings_kwargs)

        # Ensure .env is loaded
        load_dotenv(override=False)

        # Override flat env variables if present
        if openai_key := os.getenv("OPENAI_API_KEY"):
            instance.llm.openai_api_key = openai_key
        if openai_url := os.getenv("OPENAI_BASE_URL"):
            clean_url = openai_url.rstrip("/")
            if clean_url and not clean_url.endswith("/v1"):
                clean_url = f"{clean_url}/v1"
            instance.llm.openai_base_url = clean_url
        if deepseek_key := os.getenv("DEEPSEEK_API_KEY"):
            instance.llm.deepseek_api_key = deepseek_key
        if deepseek_url := os.getenv("DEEPSEEK_BASE_URL"):
            instance.llm.deepseek_base_url = deepseek_url
        if deepseek_model := os.getenv("DEEPSEEK_MODEL_NAME"):
            instance.llm.deepseek_model_name = deepseek_model
        if google_key := os.getenv("GOOGLE_API_KEY"):
            instance.llm.google_api_key = google_key
        if gemini_model := os.getenv("GEMINI_MODEL_NAME"):
            instance.llm.gemini_model_name = gemini_model
        if anthropic_key := os.getenv("ANTHROPIC_API_KEY"):
            instance.llm.anthropic_api_key = anthropic_key
        if anthropic_model := os.getenv("ANTHROPIC_MODEL_NAME"):
            instance.llm.anthropic_model_name = anthropic_model
        if ollama_url := os.getenv("OLLAMA_BASE_URL"):
            instance.llm.ollama_base_url = ollama_url
        if ollama_model := os.getenv("OLLAMA_MODEL_NAME"):
            instance.llm.ollama_model_name = ollama_model
        if default_provider := os.getenv("DEFAULT_LLM_PROVIDER"):
            try:
                instance.llm.default_provider = LLMProviderType(default_provider.lower())
            except ValueError:
                pass
        if default_model := os.getenv("DEFAULT_MODEL_NAME"):
            instance.llm.default_model = default_model
        if default_temp := os.getenv("DEFAULT_TEMPERATURE"):
            try:
                instance.llm.temperature = float(default_temp)
            except ValueError:
                pass
        if sqlite_path := os.getenv("SQLITE_DB_PATH"):
            instance.storage.sqlite_path = sqlite_path
        if checkpoint_backend := os.getenv("CHECKPOINT_BACKEND"):
            instance.storage.checkpoint_backend = checkpoint_backend
        if workspace_root := os.getenv("WORKSPACE_ROOT"):
            instance.storage.workspace_root = workspace_root
        if vector_db_path := os.getenv("VECTOR_DB_PATH"):
            instance.rag.vector_db_path = vector_db_path
        if hitl_enabled := os.getenv("HITL_ENABLED"):
            instance.security.hitl_enabled = hitl_enabled.lower() in ("true", "1", "yes")
        if hitl_auto := os.getenv("HITL_AUTO_APPROVE_READ_ONLY"):
            instance.security.auto_approve_read_only = hitl_auto.lower() in ("true", "1", "yes")
        if log_lvl := os.getenv("LOG_LEVEL"):
            instance.observability.log_level = log_lvl
        if app_env := os.getenv("APP_ENV"):
            instance.app.environment = app_env
        if app_host := os.getenv("APP_HOST"):
            instance.app.host = app_host
        if app_port := os.getenv("APP_PORT"):
            try:
                instance.app.port = int(app_port)
            except ValueError:
                pass
        if debug_val := os.getenv("DEBUG"):
            instance.app.debug = debug_val.lower() in ("true", "1", "yes")

        # Ensure directories exist
        Path(instance.storage.workspace_root).mkdir(parents=True, exist_ok=True)
        if instance.storage.checkpoint_backend == "sqlite":
            Path(instance.storage.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        if instance.rag.enabled and instance.rag.vector_db_path != ":memory:":
            Path(instance.rag.vector_db_path).parent.mkdir(parents=True, exist_ok=True)

        return instance


_global_settings: Settings | None = None


def get_settings() -> Settings:
    """Retrieve the cached global settings instance."""
    global _global_settings
    if _global_settings is None:
        _global_settings = Settings.load_from_env_and_yaml()
    return _global_settings
