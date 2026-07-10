import os
from app.config import Settings


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "1")
    s = Settings()
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.llm_model == "deepseek-chat"
    assert s.agent_max_iterations == 10
    assert s.compose_projects_dir == "/opt"


def test_dialog_memory_defaults(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "1")
    s = Settings()
    assert s.dialog_db_path == "/data/dialog.db"
    assert s.dialog_history_limit == 20


def test_deploy_allowed_default(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "1")
    s = Settings()
    assert s.deploy_allowed == ""
