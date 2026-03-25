"""Configuration management via pydantic-settings."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8020

    # Auth (empty = no auth required - fine for localhost single-user)
    api_key: str = ""
    allowed_origins: str = "chrome-extension://*,http://localhost:*,http://127.0.0.1:*"

    # Database
    sqlite_path: str = "./stockpilot.db"

    # Cache
    redis_url: str = "redis://localhost:6379"

    # LLM defaults (overridable via /api/settings endpoint)
    default_llm_provider: str = "gemini"
    default_llm_model: str = "gemini-2.0-flash"
    gemini_api_key: str = ""
    openai_api_key: str = ""
    claude_api_key: str = ""

    # Signal engine
    default_alert_interval_minutes: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
