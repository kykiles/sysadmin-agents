from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_api_key: str = Field(alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_user_id: int = Field(alias="TELEGRAM_USER_ID")
    telegram_chat_id: int = Field(default=0, alias="TELEGRAM_CHAT_ID")

    compose_projects_dir: str = Field(default="/opt", alias="COMPOSE_PROJECTS_DIR")
    agent_max_iterations: int = Field(default=10, alias="AGENT_MAX_ITERATIONS")
    confirmation_timeout_seconds: int = Field(
        default=300, alias="CONFIRMATION_TIMEOUT_SECONDS"
    )
    audit_log_path: str = Field(default="/data/audit.log", alias="AUDIT_LOG_PATH")


@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


class _SettingsProxy:
    def __getattr__(self, name: str):
        return getattr(_get_settings(), name)


settings = _SettingsProxy()
