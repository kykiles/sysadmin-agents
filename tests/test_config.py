import os
from app.config import Settings


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "1")
    s = Settings()
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.llm_model == "deepseek-chat"
    assert s.agent_max_iterations == 10
    assert s.compose_projects_dir == "/opt"
