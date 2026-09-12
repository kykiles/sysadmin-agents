from unittest import mock

import pytest

from skills.ssh.tools import _node_binaries, _ssh_argv, ssh_query, build_access_tools
from app.skills.readonly import HostAccess, is_read_only
from app.tools.base import Safety

_BASE = _node_binaries(HostAccess())


def _is_read_only(command, binaries=_BASE):
    return is_read_only(command, binaries)


async def _dry(query, command):
    """Проверка классификации без реального ssh: подменяем транспорт."""
    with mock.patch("skills.ssh.tools.shell_exec", new=mock.AsyncMock(return_value={})):
        return await query(host="10.0.0.1", command=command)


def test_readonly_covers_host_and_docker():
    assert _is_read_only(["df", "-h"])
    assert _is_read_only(["journalctl", "-u", "docker", "-n", "50", "--no-pager"])
    assert _is_read_only(["docker", "ps", "-a"])
    assert _is_read_only(["docker", "logs", "--tail", "200", "remnanode"])
    assert _is_read_only(["docker", "compose", "ps"])


def test_mutating_blocked():
    assert not _is_read_only(["docker", "restart", "remnanode"])
    assert not _is_read_only(["docker", "compose", "up", "-d"])
    assert not _is_read_only(["systemctl", "restart", "docker"])
    assert not _is_read_only(["rm", "-rf", "/"])


def test_shell_wrapper_never_readonly():
    assert not _is_read_only(["sh", "-c", "docker ps -a | grep remnanode"])
    assert not _is_read_only(["sh", "-c", "docker ps && docker restart remnanode"])


@pytest.mark.parametrize("command", [
    ["sh", "-c", "echo inspection\ntouch /tmp/f01-newline"],
    ["sh", "-c", "printf audit | uniq - /tmp/f01-uniq"],
    ["bash", "-c", "printf audit >& /tmp/f01-redirect"],
    ["bash", "-lc", "uptime"],
    ["sh", "-e", "-c", "df"],
    ["uniq", "/etc/passwd", "/tmp/out"],
    ["certbot", "certificates"],  # вне скоупа ноды
])
async def test_ssh_query_refuses_before_transport(command):
    with mock.patch("skills.ssh.tools.shell_exec", new=mock.AsyncMock(return_value={})) as ssh:
        out = await build_access_tools(HostAccess())[0].fn(host="10.0.0.1", command=command)
    assert "error" in out
    ssh.assert_not_called()


async def test_ssh_query_runs_direct_df_with_quoted_argv():
    with mock.patch("skills.ssh.tools.shell_exec", new=mock.AsyncMock(return_value={})) as ssh:
        out = await build_access_tools(HostAccess())[0].fn(host="10.0.0.1", command=["df", "-h"])
    assert "error" not in out
    ssh.assert_awaited_once()
    assert ssh.await_args.args[0][-1] == "df -h"


def test_argv_quotes_remote_command_and_defaults_user():
    argv = _ssh_argv("10.0.0.1", ["sh", "-c", "df -h | grep /"])
    assert argv[0] == "ssh"
    assert "root@10.0.0.1" in argv
    assert argv[-1] == "sh -c 'df -h | grep /'"
    assert _ssh_argv("admin@node1", ["uptime"])[-2] == "admin@node1"


def test_binaries_extend_with_other_skills():
    """observe даёт top/vmstat — на ноде они должны читаться так же, как локально."""
    assert not _is_read_only(["top", "-bn1"])
    with_observe = _node_binaries(HostAccess(binaries=frozenset({"top"})))
    assert _is_read_only(["top", "-bn1"], with_observe)
    assert not _is_read_only(["rm", "-rf", "/"], with_observe)


@pytest.mark.asyncio
async def test_ssh_query_refuses_mutating():
    res = await ssh_query("10.0.0.1", ["docker", "restart", "remnanode"], _BASE)
    assert "error" in res and "ssh_exec" in res["error"]


@pytest.mark.asyncio
async def test_query_tool_uses_full_node_scope():
    """Инструмент должен получать _node_binaries(access), а не голый access.binaries."""
    query = build_access_tools(HostAccess(binaries=frozenset({"top"})))[0].fn
    assert "error" not in await _dry(query, ["docker", "ps"])
    assert "error" not in await _dry(query, ["top", "-bn1"])
    assert "error" in await _dry(query, ["rm", "-rf", "/"])


def test_tool_safety():
    tools = {t.name: t.safety for t in build_access_tools(HostAccess())}
    assert tools["ssh_query"] is Safety.SAFE
    assert tools["ssh_exec"] is Safety.DANGEROUS
