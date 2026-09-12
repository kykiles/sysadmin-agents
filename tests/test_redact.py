from app.logging import redact

JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1dWlkIjoiYmQwMTRiNjgifQ.aBc-_123"


def test_jwt_replaced():
    out = redact(f'"-H", "Authorization: Bearer {JWT}"')
    assert JWT not in out
    assert "<redacted>" in out


def test_opaque_bearer_replaced():
    out = redact("Authorization: Bearer sk-live-abcdef123456")
    assert "sk-live" not in out


def test_plain_text_untouched():
    text = '{"returncode": 0, "stdout": "AEZA DE-01 212.113.0.1"}'
    assert redact(text) == text


def test_pgpassword_assignment_replaced():
    out = redact('{"command": ["sh", "-c", "PGPASSWORD=s3cr3tpassw0rdvalue00 psql -U postgres"]}')
    assert "s3cr3tpassw0rdvalue00" not in out
    assert "PGPASSWORD=<redacted>" in out


def test_url_credentials_replaced():
    out = redact('psql postgresql://postgres:s3cr3tpassw0rdva@127.0.0.1:5432/postgres')
    assert "s3cr3tpassw0rdva" not in out
    assert "postgresql://postgres:<redacted>@127.0.0.1:5432/postgres" in out


def test_env_dump_replaced():
    out = redact('BOT_TOKEN=123456:AAH-secret\nAPI_KEY=tvly-abc123\nDB_HOST=127.0.0.1')
    assert "AAH-secret" not in out and "tvly-abc123" not in out
    assert "DB_HOST=127.0.0.1" in out


def test_paths_and_names_untouched():
    text = '{"command": ["ls", "-la", "/data/ssh/id_ed25519"], "host": "root@1.2.3.4"}'
    assert redact(text) == text


# ---------- секреты по значению (аудит 2026-09-12, F08) ----------

def test_setting_secret_replaced_in_any_form(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "remnawave_api_key", "AUDIT_FAKE_SECRET")
    out = redact('{"stdout": "x-api: AUDIT_FAKE_SECRET", "echo": "...AUDIT_FAKE_SECRET..."}')
    assert "AUDIT_FAKE_SECRET" not in out


def test_short_setting_value_does_not_mangle_output(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "remnawave_api_key", "k")
    assert redact("docker ps -k") == "docker ps -k"


def test_registered_secret_replaced():
    from app.logging import register_secret

    register_secret("mcp-registered-key-0042")
    out = redact("ConnectError: https://mcp.example/?key=mcp-registered-key-0042")
    assert "mcp-registered-key-0042" not in out


def test_scrub_walks_nested_values(monkeypatch):
    import json
    from app.config import settings
    from app.logging import scrub

    monkeypatch.setattr(settings, "remnawave_api_key", "AUDIT_FAKE_SECRET")
    out = scrub({"nodes": [{"auth": {"token": "AUDIT_FAKE_SECRET"}}], "n": 1,
                 "argv": ("curl", "Bearer AUDIT_FAKE_SECRET"), "ok": "полезное"})
    assert "AUDIT_FAKE_SECRET" not in json.dumps(out)
    assert out["n"] == 1 and out["ok"] == "полезное"


def test_setup_logging_scrubs_every_log_field(tmp_path, monkeypatch):
    import logging
    import structlog
    from app import logging as app_logging

    monkeypatch.setattr(app_logging.settings, "audit_log_path", str(tmp_path / "audit.log"))
    root = logging.getLogger()
    handlers = list(root.handlers)
    try:
        app_logging.setup_logging()
        assert app_logging.scrub_event in structlog.get_config()["processors"]
    finally:
        structlog.reset_defaults()
        for h in root.handlers[len(handlers):]:
            root.removeHandler(h)
            h.close()
    event = app_logging.scrub_event(None, "info", {"event": "x", "url": "https://u:passw0rd123@h/"})
    assert "passw0rd123" not in event["url"]
