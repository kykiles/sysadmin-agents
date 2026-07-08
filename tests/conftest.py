import os
import pytest


@pytest.fixture(autouse=True, scope="session")
def _set_test_env():
    os.environ.setdefault("LLM_API_KEY", "test-key")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    os.environ.setdefault("TELEGRAM_USER_ID", "1")
