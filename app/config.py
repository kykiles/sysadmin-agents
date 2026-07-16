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
    agent_max_iterations: int = Field(default=25, alias="AGENT_MAX_ITERATIONS")
    confirmation_timeout_seconds: int = Field(
        default=300, alias="CONFIRMATION_TIMEOUT_SECONDS"
    )
    audit_log_path: str = Field(default="/data/audit.log", alias="AUDIT_LOG_PATH")
    audit_trail_path: str = Field(default="/data/audit.jsonl", alias="AUDIT_TRAIL_PATH")
    dialog_db_path: str = Field(default="/data/dialog.db", alias="DIALOG_DB_PATH")
    dialog_history_limit: int = Field(default=20, alias="DIALOG_HISTORY_LIMIT")
    dialog_history_token_budget: int = Field(default=4000, alias="DIALOG_HISTORY_TOKEN_BUDGET")
    dialog_retention_days: int = Field(default=90, alias="DIALOG_RETENTION_DAYS")
    reports_dir: str = Field(default="/data/reports", alias="REPORTS_DIR")
    journal_enabled: bool = Field(default=True, alias="JOURNAL_ENABLED")
    journal_db_path: str = Field(default="/data/tasks.db", alias="JOURNAL_DB_PATH")
    deploy_allowed: str = Field(default="", alias="DEPLOY_ALLOWED")
    backup_allowed: str = Field(default="", alias="BACKUP_ALLOWED")
    backup_dir: str = Field(default="/var/backups/sysadmin", alias="BACKUP_DIR")
    backup_keep: int = Field(default=7, alias="BACKUP_KEEP")
    remnawave_base_url: str = Field(default="", alias="REMNAWAVE_BASE_URL")
    remnawave_api_key: str = Field(default="", alias="REMNAWAVE_API_KEY")
    remnawave_timeout: int = Field(default=30, alias="REMNAWAVE_TIMEOUT")

    monitor_enabled: bool = Field(default=False, alias="MONITOR_ENABLED")
    monitor_interval_seconds: int = Field(default=300, alias="MONITOR_INTERVAL_SECONDS")
    monitor_db_path: str = Field(default="/data/monitoring.db", alias="MONITOR_DB_PATH")
    monitor_containers: str = Field(default="", alias="MONITOR_CONTAINERS")
    monitor_disk_pct: float = Field(default=90.0, alias="MONITOR_DISK_PCT")
    monitor_mem_min_mb: int = Field(default=200, alias="MONITOR_MEM_MIN_MB")
    monitor_load_per_cpu: float = Field(default=2.0, alias="MONITOR_LOAD_PER_CPU")
    monitor_tls_endpoints: str = Field(default="", alias="MONITOR_TLS_ENDPOINTS")
    monitor_tls_warn_days: int = Field(default=14, alias="MONITOR_TLS_WARN_DAYS")
    monitor_tls_every_ticks: int = Field(default=12, alias="MONITOR_TLS_EVERY_TICKS")


@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


class _SettingsProxy:
    def __getattr__(self, name: str):
        return getattr(_get_settings(), name)


settings = _SettingsProxy()
