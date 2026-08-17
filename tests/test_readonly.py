"""Единый классификатор read-only и скоупы скилов.

Заменяет четыре набора тестов (host/observe/tls/security), которые проверяли
пять копий одной и той же логики.
"""
import pytest

from skills.host.tools import ACCESS as HOST
from skills.observe.tools import ACCESS as OBSERVE
from app.skills.readonly import (
    HostAccess, KNOWN_BINARIES, build_host_tools, is_read_only,
)
from skills.security.tools import ACCESS as SECURITY
from skills.tls.tools import ACCESS as TLS
from app.tools.base import Safety

ALL = KNOWN_BINARIES


def ro(*command: str) -> bool:
    """Читающая ли команда, если разрешены все известные бинарники."""
    return is_read_only(list(command), ALL)


# ---------- классификация аргументов ----------

def test_pure_readonly_binaries():
    assert ro("df", "-h")
    assert ro("ss", "-tlnp")
    assert ro("uptime")
    assert ro("vmstat", "1", "3")
    assert ro("ps", "aux", "--sort=-%cpu")


def test_iptables_listing_vs_mutating():
    assert ro("iptables", "-L", "-n", "-v")
    assert ro("iptables", "-S")
    assert not ro("iptables", "-A", "INPUT", "-j", "DROP")
    assert not ro("iptables", "-F")
    assert not ro("iptables", "-P", "INPUT", "DROP")


def test_systemctl_readonly_vs_mutating():
    assert ro("systemctl", "status", "docker")
    assert ro("systemctl", "list-timers")
    assert not ro("systemctl", "stop", "docker")
    assert not ro("systemctl", "restart", "nginx")
    assert not ro("systemctl", "reload", "nginx")


def test_ip_show_vs_mutating():
    assert ro("ip", "addr")
    assert ro("ip", "route")
    assert not ro("ip", "addr", "add", "10.0.0.1/24", "dev", "eth0")
    assert not ro("ip", "route", "flush", "cache")


def test_journalctl_read_vs_mutating():
    assert ro("journalctl", "-u", "nginx", "-n", "100", "--no-pager")
    assert not ro("journalctl", "--vacuum-size", "100M")


def test_crontab_listing_only():
    assert ro("crontab", "-l")
    assert not ro("crontab", "-r")
    assert not ro("crontab", "-")


def test_certbot_certificates_only():
    assert ro("certbot", "certificates")
    assert not ro("certbot", "renew")


def test_audit_binaries():
    assert ro("fail2ban-client", "status")
    assert ro("ufw", "status")
    assert not ro("ufw", "enable")
    assert ro("sshd", "-T")
    assert not ro("sshd")


def test_apt_readonly():
    assert ro("apt", "list", "--upgradable")
    assert ro("apt-get", "-s", "upgrade")
    assert not ro("apt-get", "upgrade")


def test_openssl_inspection():
    assert ro("openssl", "x509", "-enddate", "-noout", "-in", "/x/fullchain.pem")
    assert ro("find", "/root/.acme.sh", "-name", "*.cer")


def test_unknown_binary_blocked():
    assert not ro("rm", "-rf", "/")
    assert not ro()


# ---------- обёртка sh -c ----------

def test_sh_wrapper_readonly_allowed():
    assert ro("sh", "-c", "df -h | grep -i /")
    assert ro("bash", "-c", "ss -tlnp && free -m")


def test_sh_wrapper_mutating_blocked():
    assert not ro("sh", "-c", "df -h && systemctl restart docker")
    assert not ro("bash", "-c", "rm -rf /tmp/x")
    assert not ro("sh", "-c", "certbot renew && systemctl reload nginx")


def test_sh_wrapper_cert_discovery_allowed():
    # реальный паттерн из лога: echo + ls + for-цикл с openssl, всё read-only
    assert ro(
        "sh", "-c",
        'echo "---LIVE---" && ls -la /etc/letsencrypt/live/ 2>/dev/null && '
        'for dir in /etc/letsencrypt/live/*/; do echo "=== $dir ===" && '
        'openssl x509 -in "${dir}cert.pem" -noout -subject -dates 2>/dev/null; done',
    )
    assert ro("sh", "-c", "certbot certificates 2>&1 | grep -i domain")


def test_sh_wrapper_write_redirection_blocked():
    assert not ro("sh", "-c", "openssl x509 -in a.pem > /etc/out.txt")


# ---------- скоуп: скил ограничивает набор бинарников ----------

@pytest.mark.parametrize("access,allowed,denied", [
    (HOST, ["iptables", "-L"], ["certbot", "certificates"]),
    (TLS, ["certbot", "certificates"], ["iptables", "-L"]),
    (SECURITY, ["fail2ban-client", "status"], ["certbot", "certificates"]),
    (OBSERVE, ["vmstat", "1"], ["iptables", "-L"]),
])
def test_scope_limits_binaries(access, allowed, denied):
    assert is_read_only(allowed, access.binaries)
    assert not is_read_only(denied, access.binaries)


@pytest.mark.parametrize("access", [HOST, OBSERVE])
def test_nproc_and_echo_pipeline(access):
    # агент собирает сводку одним sh -c 'echo ...; uptime; nproc' — не должно упираться в скоуп
    assert is_read_only(["nproc"], access.binaries)
    assert is_read_only(["sh", "-c", "echo '=== load ==='; uptime; nproc"], access.binaries)


def test_scope_applies_inside_sh_wrapper():
    # обёртка не обходит скоуп: certbot недоступен агенту со скилом host
    assert not is_read_only(["sh", "-c", "certbot certificates"], HOST.binaries)


def test_union_of_scopes_sees_both():
    both = TLS | SECURITY
    assert is_read_only(["certbot", "certificates"], both.binaries)
    assert is_read_only(["fail2ban-client", "status"], both.binaries)
    assert both.exec_allowed is True  # tls разрешает изменяющие команды


def test_union_without_exec_stays_readonly():
    both = OBSERVE | SECURITY
    assert both.exec_allowed is False
    assert [t.name for t in build_host_tools(both)] == ["host_query"]


# ---------- сборка инструментов ----------

def test_exec_tool_only_when_allowed():
    names = [t.name for t in build_host_tools(HOST)]
    assert names == ["host_query", "shell_exec"]
    by_name = {t.name: t for t in build_host_tools(HOST)}
    assert by_name["host_query"].safety is Safety.SAFE
    assert by_name["shell_exec"].safety is Safety.DANGEROUS


def test_empty_access_yields_no_tools():
    assert build_host_tools(HostAccess()) == []


def test_allowed_binaries_are_listed_in_description():
    (query, _exec) = build_host_tools(HOST)
    assert "iptables" in query.description


async def test_host_query_rejects_and_runs(monkeypatch):
    import app.skills.readonly as ro_mod

    async def fake_host_exec(command):
        return {"command": command, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ro_mod, "host_exec", fake_host_exec)
    query = build_host_tools(HOST)[0]

    refused = await query.fn(command=["systemctl", "stop", "docker"])
    assert "error" in refused

    ok = await query.fn(command=["df", "-h"])
    assert ok["returncode"] == 0


def test_docker_skill_allows_reading_logs_via_host():
    """Агрегация логов (docker logs | grep -c) уходила в отказ — docker не был в binaries."""
    from skills.docker.tools import ACCESS
    assert is_read_only(["sh", "-c", "docker logs --tail 100 x 2>&1 | grep -c error"], ACCESS.binaries)
    assert not is_read_only(["docker", "rm", "-f", "x"], ACCESS.binaries)
