from app.skills.tls.tools import _is_read_only, tls_query, build_tools
from app.tools.base import Safety


def test_inspection_binaries_allowed():
    assert _is_read_only(["openssl", "x509", "-enddate", "-noout", "-in", "/x/fullchain.pem"])
    assert _is_read_only(["ls", "-la", "/etc/letsencrypt/live"])
    assert _is_read_only(["find", "/root/.acme.sh", "-name", "*.cer"])
    assert _is_read_only(["systemctl", "list-timers"])


def test_certbot_certificates_only():
    assert _is_read_only(["certbot", "certificates"])
    assert not _is_read_only(["certbot", "renew"])


def test_crontab_listing_only():
    assert _is_read_only(["crontab", "-l"])
    assert not _is_read_only(["crontab", "-r"])


def test_mutating_blocked():
    assert not _is_read_only(["systemctl", "reload", "nginx"])
    assert not _is_read_only(["rm", "-rf", "/etc/letsencrypt"])
    assert not _is_read_only([])


async def test_tls_query_rejects_non_readonly():
    out = await tls_query(command=["certbot", "renew"])
    assert "error" in out


async def test_tls_query_runs_via_host_exec(monkeypatch):
    import app.skills.tls.tools as tt

    async def fake_host_exec(command):
        return {"command": command, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(tt, "host_exec", fake_host_exec)
    out = await tls_query(command=["certbot", "certificates"])
    assert out["returncode"] == 0


def test_tls_renew_is_dangerous():
    tools = {t.name: t for t in build_tools()}
    assert tools["tls_query"].safety is Safety.SAFE
    assert tools["tls_check"].safety is Safety.SAFE
    assert tools["tls_renew"].safety is Safety.DANGEROUS
