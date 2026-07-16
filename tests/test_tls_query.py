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
    assert tools["tls_report"].safety is Safety.SAFE
    assert tools["tls_query"].safety is Safety.SAFE
    assert tools["tls_check"].safety is Safety.SAFE
    assert tools["tls_renew"].safety is Safety.DANGEROUS


def test_sh_wrapper_cert_discovery_allowed():
    # реальный паттерн из лога: echo + ls + for-цикл с openssl, всё read-only
    assert _is_read_only([
        "sh", "-c",
        'echo "---LIVE---" && ls -la /etc/letsencrypt/live/ 2>/dev/null && '
        'for dir in /etc/letsencrypt/live/*/; do echo "=== $dir ===" && '
        'openssl x509 -in "${dir}cert.pem" -noout -subject -dates 2>/dev/null; done',
    ])
    # пайп в grep/head тоже допустим
    assert _is_read_only(["sh", "-c", "certbot certificates 2>&1 | grep -i domain"])


def test_sh_wrapper_certbot_renew_blocked():
    # certbot renew внутри обёртки — изменяющая, не read-only
    assert not _is_read_only(["sh", "-c", "certbot renew && systemctl reload nginx"])


def test_sh_wrapper_write_redirection_blocked():
    # запись в реальный файл запрещена, даже если сама команда read-only
    assert not _is_read_only(["sh", "-c", "openssl x509 -in a.pem > /etc/out.txt"])


async def test_tls_report_runs_via_host_shell(monkeypatch):
    import app.skills.tls.tools as tt

    captured = {}

    async def fake_host_shell(script):
        captured["script"] = script
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(tt, "host_shell", fake_host_shell)
    out = await tt.tls_report()
    assert out["returncode"] == 0
    assert "certbot certificates" in captured["script"]
