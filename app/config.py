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

    compose_projects_dir: str = Field(default="/opt", alias="COMPOSE_PROJECTS_DIR")
    shell_timeout_seconds: int = Field(default=120, alias="SHELL_TIMEOUT_SECONDS")
    agent_max_iterations: int = Field(default=10, alias="AGENT_MAX_ITERATIONS")
    confirmation_timeout_seconds: int = Field(
        default=300, alias="CONFIRMATION_TIMEOUT_SECONDS"
    )
    audit_log_path: str = Field(default="/data/audit.log", alias="AUDIT_LOG_PATH")
    audit_trail_path: str = Field(default="/data/audit.jsonl", alias="AUDIT_TRAIL_PATH")
    dialog_db_path: str = Field(default="/data/dialog.db", alias="DIALOG_DB_PATH")
    dialog_history_limit: int = Field(default=20, alias="DIALOG_HISTORY_LIMIT")
    dialog_history_token_budget: int = Field(default=4000, alias="DIALOG_HISTORY_TOKEN_BUDGET")
    dialog_retention_days: int = Field(default=90, alias="DIALOG_RETENTION_DAYS")
    deploy_allowed: str = Field(default="", alias="DEPLOY_ALLOWED")
    backup_allowed: str = Field(default="", alias="BACKUP_ALLOWED")
    backup_dir: str = Field(default="/var/backups/sysadmin", alias="BACKUP_DIR")
    backup_keep: int = Field(default=7, alias="BACKUP_KEEP")
    remnawave_base_url: str = Field(default="", alias="REMNAWAVE_BASE_URL")
    remnawave_api_key: str = Field(default="", alias="REMNAWAVE_API_KEY")
    remnawave_timeout: int = Field(default=30, alias="REMNAWAVE_TIMEOUT")


@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


class _SettingsProxy:
    def __getattr__(self, name: str):
        return getattr(_get_settings(), name)


settings = _SettingsProxy()
