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


# ---------- оболочки не бывают read-only (аудит 2026-09-12, F01) ----------

# Три воспроизведения из аудита: newline, `uniq in out`, `>&` в файл.
F01_CASES = [
    ["sh", "-c", "echo inspection\ntouch /tmp/f01-newline"],
    ["sh", "-c", "printf audit | uniq - /tmp/f01-uniq"],
    ["bash", "-c", "printf audit >& /tmp/f01-redirect"],
]

SHELL_CASES = F01_CASES + [
    ["sh", "-c", "df -h"],
    ["bash", "-c", "ss -tlnp && free -m"],
    ["bash", "-lc", "uptime"],
    ["sh", "-e", "-c", "df -h"],
    ["bash", "--norc", "-c", "df"],
    ["dash", "-c", "df"],
    ["sh", "/tmp/script.sh"],
    ["sh"],
    ["uniq", "/etc/passwd", "/tmp/out"],
    ["/bin/sh", "-c", "df"],
]


@pytest.mark.parametrize("command", SHELL_CASES)
def test_shell_never_readonly(command):
    assert not ro(*command)


def test_shell_refused_even_if_listed_in_binaries():
    """Оболочка в скоупе скила не делает её читающей: проверки аргументов у неё нет."""
    assert not is_read_only(["sh", "-c", "df"], frozenset({"sh", "bash", "df"}))
    assert not is_read_only(["bash", "-c", "df"], frozenset({"sh", "bash", "df"}))


@pytest.mark.parametrize("command", SHELL_CASES + [
    ["certbot", "certificates"],  # вне HostAccess скила host
    ["rm", "-rf", "/tmp/x"],
])
async def test_host_query_refuses_before_executor(monkeypatch, command):
    import app.skills.readonly as ro_mod

    calls = []

    async def fake_host_exec(cmd):
        calls.append(cmd)
        return {"command": cmd, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(ro_mod, "host_exec", fake_host_exec)
    query = build_host_tools(HOST)[0]
    out = await query.fn(command=command)
    assert "error" in out
    assert calls == []


async def test_host_query_runs_direct_df(monkeypatch):
    import app.skills.readonly as ro_mod

    calls = []

    async def fake_host_exec(cmd):
        calls.append(cmd)
        return {"command": cmd, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ro_mod, "host_exec", fake_host_exec)
    out = await build_host_tools(HOST)[0].fn(command=["df", "-h"])
    assert out["returncode"] == 0
    assert calls == [["df", "-h"]]


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
def test_nproc_and_uptime_direct(access):
    # сводка собирается отдельными вызовами, а не одним sh -c
    assert is_read_only(["nproc"], access.binaries)
    assert is_read_only(["uptime"], access.binaries)
    assert not is_read_only(["sh", "-c", "echo '=== load ==='; uptime; nproc"], access.binaries)


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
    """Чтение логов через host_query: docker должен быть в binaries скила docker."""
    from skills.docker.tools import ACCESS
    assert is_read_only(["docker", "logs", "--tail", "100", "x"], ACCESS.binaries)
    assert not is_read_only(["sh", "-c", "docker logs --tail 100 x 2>&1 | grep -c error"], ACCESS.binaries)
    assert not is_read_only(["docker", "rm", "-f", "x"], ACCESS.binaries)


# ---------- контракт argv: изменяющие режимы разрешённых утилит (аудит 2026-09-12, F02) ----------

MUTATING = [
    # воспроизведения аудита
    ["ss", "-K"], ["ss", "-tK"], ["ss", "--kill"], ["ss", "-D", "/tmp/dump"],
    ["hostname", "audit-new-host"], ["hostname", "-F", "/tmp/name"],
    ["date", "-s", "2030-01-01"], ["date", "--set=2030-01-01"], ["date", "010100002030"],
    ["date", "--iso-8601", "010100002030"],  # опция с необязательным значением
    ["journalctl", "--vacuum-time=1s"], ["journalctl", "--vacuum-time", "1s"],
    ["journalctl", "--vacuum-t=1s"],  # сокращение getopt_long
    ["journalctl", "--rotate"], ["journalctl", "--flush"],
    ["journalctl", "--cursor-file=/tmp/c"], ["journalctl", "-f"],
    # остальные утилиты
    ["dmesg", "-C"], ["dmesg", "-c"], ["dmesg", "--clear"], ["dmesg", "-Tc"], ["dmesg", "-n", "1"],
    ["lastlog", "-C", "-u", "root"], ["lastlog", "--set", "-u", "root"],
    ["df", "--sync"], ["free", "-s", "1"], ["tail", "-f", "/var/log/syslog"],
    ["lsof", "-D", "b"], ["lsof", "+m"],
    ["iptables", "-F"], ["iptables", "-L", "-Z"], ["iptables", "-nvLZ"],
    ["iptables", "-A", "INPUT", "-j", "DROP"], ["ip6tables", "-P", "INPUT", "DROP"],
    ["ip", "netns", "exec", "x", "rm", "-rf", "/"], ["ip", "-b", "/tmp/cmds"],
    ["ip", "-batch", "/tmp/cmds"], ["ip", "-force", "addr"], ["ip", "-n", "x", "addr"],
    ["ip", "link", "set", "eth0", "down"], ["ip", "-4", "addr", "add", "10.0.0.1/24", "dev", "eth0"],
    ["find", "/", "-delete"], ["find", "/", "-exec", "rm", "{}", ";"],
    ["find", "/", "-fprint", "/tmp/x"], ["find", "/", "-okdir", "rm", ";"],
    ["openssl", "x509", "-in", "a.pem", "-out", "b.pem"], ["openssl", "req", "-new"],
    ["openssl", "x509", "-in", "a.pem", "-signkey", "k.pem"], ["openssl", "s_client"],
    ["crontab", "-r"], ["crontab", "-l", "-r"], ["crontab", "/tmp/cron"], ["crontab", "-e"],
    ["systemctl", "-H", "node", "status", "x"], ["systemctl", "stop", "docker"],
    ["systemctl", "status", "x", "--now"], ["systemctl"],
    ["apt-get", "-s", "-o", "APT::Get::Simulate=false", "install", "x"],
    ["apt-get", "install", "x"], ["apt", "-c", "/tmp/apt.conf", "-s", "upgrade"],
    ["sshd"], ["sshd", "-D"], ["sshd", "-T", "-E", "/tmp/log"],
    ["certbot", "renew"], ["certbot", "certificates", "--config-dir", "/tmp"],
    ["ufw", "enable"], ["fail2ban-client", "set", "sshd", "banip", "1.2.3.4"],
    ["fail2ban-client", "-i"],
    ["docker", "-H", "tcp://x", "ps"], ["docker", "exec", "x", "sh"],
    ["docker", "logs", "-f", "x"], ["docker", "compose", "up", "-d"],
    ["docker", "compose", "-f", "x.yml", "down"],
    # неизвестные флаги и сокращённые формы
    ["df", "--frobnicate"], ["ls", "--color", "x"], ["head", "-20", "/etc/passwd"],
    ["grep", "--color", "x", "/etc/passwd"], ["uptime", "extra"],
    # скрытые вторые подкоманды и `--`
    ["ufw", "status", "reset"], ["certbot", "certificates", "renew"],
    ["fail2ban-client", "status", "sshd", "unban", "--all"],
    ["fail2ban-client", "status", "sshd", "set", "sshd", "banip", "1.2.3.4"],
    ["ip", "addr", "show", "--", "flush"], ["systemctl", "--", "stop", "x"],
    ["docker", "--", "rm", "x"], ["docker", "ps", "restart"], ["apt", "list", "--", "install"],
    ["iptables", "-L", "--", "-F"],
]

# Формы из плейбуков host/observe/security/tls/ssh и их тестов.
PLAYBOOK_FORMS = [
    ["iptables", "-L", "-n", "-v"], ["iptables", "-S"], ["iptables", "-L", "INPUT", "--line-numbers"],
    ["iptables", "-nvL"], ["iptables", "-t", "nat", "-L", "-n"],
    ["systemctl", "status", "docker"], ["systemctl", "status", "nginx", "--no-pager"],
    ["systemctl", "list-timers", "--all"], ["systemctl", "is-active", "docker"],
    ["journalctl", "-u", "nginx", "-n", "100", "--no-pager"],
    ["journalctl", "-u", "docker", "-n", "50", "--no-pager"],
    ["journalctl", "--since=-1h", "-p", "err", "--no-pager"],
    ["df", "-h"], ["du", "-h", "-d", "1", "/var/log"], ["du", "-sh", "/opt"],
    ["free", "-m"], ["uptime"], ["vmstat", "1", "3"], ["iostat", "-x", "1", "3"],
    ["mpstat", "-P", "ALL", "1", "1"],
    ["ps", "aux", "--sort=-%cpu"], ["ps", "aux", "--sort=-%mem"], ["ps", "-eo", "pid,comm,%cpu"],
    ["top", "-bn1"], ["top", "-b", "-n1"], ["top", "-b", "-n", "1"],
    ["dmesg", "-T", "--level=err,warn"], ["dmesg", "-T"],
    ["ss", "-tlnp"], ["ss", "-s"], ["ip", "addr"], ["ip", "route"], ["ip", "-br", "a"],
    ["ip", "-4", "addr", "show", "dev", "eth0"], ["ip", "route", "get", "1.1.1.1"],
    ["tail", "-n", "100", "/var/log/nginx/error.log"], ["head", "-n", "50", "/etc/passwd"],
    ["cat", "/etc/os-release"], ["grep", "-i", "error", "/var/log/syslog"],
    ["zcat", "/var/log/syslog.2.gz"], ["wc", "-l", "/etc/passwd"],
    ["sshd", "-T"], ["fail2ban-client", "status"], ["fail2ban-client", "status", "sshd"],
    ["ufw", "status"], ["ufw", "status", "verbose"],
    ["apt", "list", "--upgradable"], ["apt-get", "-s", "upgrade"],
    ["certbot", "certificates"], ["certbot", "certificates", "--cert-name", "example.com"],
    ["openssl", "x509", "-enddate", "-noout", "-in", "/x/fullchain.pem"],
    ["openssl", "x509", "-in", "cert.pem", "-noout", "-subject", "-issuer", "-dates"],
    ["find", "/root/.acme.sh", "-maxdepth", "2", "-name", "*.cer"],
    ["find", "/var/log", "-mtime", "-7", "-size", "+10M", "-type", "f"],
    ["crontab", "-l"], ["ls", "-la", "/etc/cron.d"], ["ls", "-la", "/etc/letsencrypt/live"],
    ["stat", "/etc/passwd"], ["readlink", "-f", "/etc/localtime"], ["test", "-f", "/etc/passwd"],
    ["hostname"], ["hostname", "-f"], ["date"], ["date", "+%s"], ["uname", "-a"], ["nproc"],
    ["id"], ["who"], ["w"], ["lastlog"], ["getent", "passwd", "root"], ["lsblk"], ["lscpu"],
    ["lsof", "-i", ":443"], ["lsof", "-nP", "-i", ":443"], ["echo", "hi"],
    ["docker", "ps", "-a"], ["docker", "logs", "--tail", "200", "remnanode"],
    ["docker", "logs", "--tail=200", "remnanode"], ["docker", "compose", "ps"],
    ["docker", "stats", "--no-stream", "remnanode"], ["docker", "inspect", "remnanode"],
]


@pytest.mark.parametrize("command", MUTATING)
def test_mutating_forms_refused(command):
    assert not ro(*command)


@pytest.mark.parametrize("command", PLAYBOOK_FORMS)
def test_playbook_forms_allowed(command):
    assert ro(*command)


@pytest.mark.parametrize("command", MUTATING)
async def test_mutating_forms_not_dispatched_locally(monkeypatch, command):
    import app.skills.readonly as ro_mod

    calls = []

    async def fake_host_exec(cmd):
        calls.append(cmd)
        return {"returncode": 0}

    monkeypatch.setattr(ro_mod, "host_exec", fake_host_exec)
    query = build_host_tools(HostAccess(binaries=KNOWN_BINARIES))[0]
    assert "error" in await query.fn(command=command)
    assert calls == []


@pytest.mark.parametrize("command", MUTATING)
async def test_mutating_forms_not_dispatched_over_ssh(command):
    from unittest import mock
    from skills.ssh.tools import build_access_tools

    query = build_access_tools(HostAccess(binaries=KNOWN_BINARIES))[0]
    with mock.patch("skills.ssh.tools.shell_exec", new=mock.AsyncMock(return_value={})) as ssh:
        assert "error" in await query.fn(host="10.0.0.1", command=command)
    ssh.assert_not_called()


def test_every_skill_binary_has_a_contract():
    """Бинарник в ACCESS без контракта молча отказывал бы всегда."""
    from skills.docker.tools import ACCESS as DOCKER
    for access in (HOST, OBSERVE, SECURITY, TLS, DOCKER):
        assert access.binaries <= KNOWN_BINARIES
