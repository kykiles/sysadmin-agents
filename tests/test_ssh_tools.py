import pytest

from skills.ssh.tools import _node_binaries, _ssh_argv, ssh_query, build_access_tools
from app.skills.readonly import HostAccess, is_read_only
from app.tools.base import Safety

_BASE = _node_binaries(HostAccess())


def _is_read_only(command, binaries=_BASE):
    return is_read_only(command, binaries)


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


def test_wrapped_pipeline_still_classified():
    assert _is_read_only(["sh", "-c", "docker ps -a | grep remnanode"])
    assert not _is_read_only(["sh", "-c", "docker ps && docker restart remnanode"])


def test_argv_quotes_remote_command_and_defaults_user():
    argv = _ssh_argv("10.0.0.1", ["sh", "-c", "df -h | grep /"])
    assert argv[0] == "ssh"
    assert "root@10.0.0.1" in argv
    assert argv[-1] == "sh -c 'df -h | grep /'"
    assert _ssh_argv("admin@node1", ["uptime"])[-2] == "admin@node1"


def test_binaries_extend_with_other_skills():
    """observe даёт top/vmstat — на ноде они должны читаться так же, как локально."""
    assert not _is_read_only(["sh", "-c", "uptime; top -bn1 | head -8"])
    with_observe = _node_binaries(HostAccess(binaries=frozenset({"top"})))
    assert _is_read_only(["sh", "-c", "uptime; top -bn1 | head -8"], with_observe)
    assert not _is_read_only(["rm", "-rf", "/"], with_observe)


@pytest.mark.asyncio
async def test_ssh_query_refuses_mutating():
    res = await ssh_query("10.0.0.1", ["docker", "restart", "remnanode"], _BASE)
    assert "error" in res and "ssh_exec" in res["error"]


def test_tool_safety():
    tools = {t.name: t.safety for t in build_access_tools(HostAccess())}
    assert tools["ssh_query"] is Safety.SAFE
    assert tools["ssh_exec"] is Safety.DANGEROUS
