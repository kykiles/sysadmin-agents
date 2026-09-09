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
