"""tls_check — единственный собственный инструмент скила security.

Классификация read-only переехала в test_readonly.py.
"""
from skills.security.tools import tls_check, build_tools
from app.tools.base import Safety


async def test_tls_check_validates_endpoint():
    out = await tls_check(endpoint="not-an-endpoint")
    assert "error" in out


async def test_tls_check_builds_openssl(monkeypatch):
    import skills.security.tools as st
    captured = {}

    async def fake_host_shell(script):
        captured["script"] = script
        return {"returncode": 0, "stdout": "notAfter=Jan 1 2027", "stderr": ""}

    monkeypatch.setattr(st, "host_shell", fake_host_shell)
    out = await tls_check(endpoint="example.com:443")
    assert "openssl s_client -connect example.com:443 -servername example.com" in captured["script"]
    assert out["certificate"] == "notAfter=Jan 1 2027"


def test_all_security_tools_safe():
    for t in build_tools():
        assert t.safety is Safety.SAFE
