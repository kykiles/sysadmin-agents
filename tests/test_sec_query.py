from app.skills.security.tools import _is_read_only, sec_query, tls_check, build_tools
from app.tools.base import Safety


def test_audit_binaries_allowed():
    assert _is_read_only(["ss", "-tlnp"])
    assert _is_read_only(["fail2ban-client", "status"])
    assert _is_read_only(["ufw", "status"])
    assert _is_read_only(["systemctl", "status", "sshd"])


def test_sshd_dump_only():
    assert _is_read_only(["sshd", "-T"])
    assert not _is_read_only(["sshd"])


def test_iptables_listing_vs_mutating():
    assert _is_read_only(["iptables", "-S"])
    assert not _is_read_only(["iptables", "-A", "INPUT", "-j", "DROP"])


def test_apt_readonly():
    assert _is_read_only(["apt", "list", "--upgradable"])
    assert _is_read_only(["apt-get", "-s", "upgrade"])
    assert not _is_read_only(["apt-get", "upgrade"])


def test_unknown_blocked():
    assert not _is_read_only(["systemctl", "restart", "sshd"])
    assert not _is_read_only([])


async def test_sec_query_rejects_non_readonly():
    out = await sec_query(command=["apt-get", "upgrade"])
    assert "error" in out


async def test_sec_query_runs_via_host_exec(monkeypatch):
    import app.skills.security.tools as st

    async def fake_host_exec(command):
        return {"command": command, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(st, "host_exec", fake_host_exec)
    out = await sec_query(command=["ss", "-tlnp"])
    assert out["returncode"] == 0


async def test_tls_check_validates_endpoint():
    out = await tls_check(endpoint="not-an-endpoint")
    assert "error" in out


async def test_tls_check_builds_openssl(monkeypatch):
    import app.skills.security.tools as st
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
