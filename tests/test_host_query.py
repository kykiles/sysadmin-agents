import pytest

from app.skills.host.tools import _is_read_only, host_query, build_tools
from app.tools.base import Safety


def test_pure_readonly_binaries():
    assert _is_read_only(["df", "-h"])
    assert _is_read_only(["ss", "-tlnp"])
    assert _is_read_only(["uptime"])


def test_iptables_listing_allowed():
    assert _is_read_only(["iptables", "-L", "-n", "-v"])
    assert _is_read_only(["iptables", "-S"])


def test_iptables_mutating_blocked():
    assert not _is_read_only(["iptables", "-A", "INPUT", "-j", "DROP"])
    assert not _is_read_only(["iptables", "-F"])
    assert not _is_read_only(["iptables", "-P", "INPUT", "DROP"])


def test_systemctl_readonly_vs_mutating():
    assert _is_read_only(["systemctl", "status", "docker"])
    assert not _is_read_only(["systemctl", "stop", "docker"])
    assert not _is_read_only(["systemctl", "restart", "nginx"])


def test_ip_show_vs_mutating():
    assert _is_read_only(["ip", "addr"])
    assert _is_read_only(["ip", "route"])
    assert not _is_read_only(["ip", "addr", "add", "10.0.0.1/24", "dev", "eth0"])
    assert not _is_read_only(["ip", "route", "flush", "cache"])


def test_unknown_binary_blocked():
    assert not _is_read_only(["rm", "-rf", "/"])
    assert not _is_read_only([])


def test_sh_wrapper_readonly_allowed():
    # обёртка sh -c с пайпом read-only команд и текстовым фильтром
    assert _is_read_only(["sh", "-c", "df -h | grep -i /"])
    assert _is_read_only(["bash", "-c", "ss -tlnp && free -m"])


def test_sh_wrapper_mutating_blocked():
    # хотя бы одна изменяющая команда внутри — весь скрипт не read-only
    assert not _is_read_only(["sh", "-c", "df -h && systemctl restart docker"])
    assert not _is_read_only(["bash", "-c", "rm -rf /tmp/x"])


async def test_host_query_rejects_non_readonly():
    out = await host_query(command=["systemctl", "stop", "docker"])
    assert "error" in out


async def test_host_query_runs_readonly(monkeypatch):
    import app.skills.host.tools as ht

    async def fake_shell(command):
        return {"command": command, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ht, "shell_exec", fake_shell)
    out = await host_query(command=["df", "-h"])
    assert out["returncode"] == 0


def test_host_skill_exposes_host_query_safe():
    by_name = {t.name: t for t in build_tools()}
    assert by_name["host_query"].safety is Safety.SAFE
    assert by_name["shell_exec"].safety is Safety.DANGEROUS
