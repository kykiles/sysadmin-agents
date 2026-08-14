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
