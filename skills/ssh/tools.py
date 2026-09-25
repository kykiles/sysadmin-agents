"""SSH как транспорт: один инструмент — одна задача (доставить команду на ноду).

Классификация та же, что для локального хоста: команда на удалённой ноде опасна
ровно так же. Отличие одно — на ноде нет docker-сокета, поэтому read-only
подкоманды `docker` доступны здесь и только здесь.
"""
import shlex

from pydantic import BaseModel, Field

from app.config import settings
from skills.host.tools import ACCESS as _HOST_ACCESS
from app.skills.network import is_allowed, refusal_reason
from app.skills.readonly import HostAccess, is_read_only, refusal
from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec


def _node_binaries(access: HostAccess) -> frozenset[str]:
    """Что читаем на ноде: базовый набор хоста плюс то, что принесли остальные
    выданные агенту скилы (observe даёт top/vmstat, tls — openssl). Иначе одна и
    та же команда проходила локально и отвергалась на ноде."""
    return _HOST_ACCESS.binaries | access.binaries | {"docker"}


# Ни user, ни host не начинаются с `-`: иначе ssh прочтёт значение как свою опцию
# (`-oProxyCommand=…` — исполнение в контейнере без подтверждения, аудит Б1).
# Проверка в модели — отказ ещё до подтверждения и до транспорта.
_HOST_RE = r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$"


class SshParams(BaseModel):
    host: str = Field(description="node IP or hostname (may be user@host)", pattern=_HOST_RE)
    command: list[str] = Field(description="command argv to run on the node")


def _ssh_argv(host: str, command: list[str]) -> list[str]:
    target = host if "@" in host else f"{settings.ssh_user}@{host}"
    return [
        "ssh", "-i", settings.ssh_key_path,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        # Мультиплексирование: первое подключение держит канал, остальные команды
        # (и другие агенты) переиспользуют его без нового хендшейка.
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/ssh-%r@%h:%p",
        "-o", "ControlPersist=60s",
        "--",  # второй слой к _HOST_RE: цель не станет опцией ни при каком значении
        target,
        " ".join(shlex.quote(a) for a in command),
    ]


async def ssh_query(host: str, command: list[str], binaries: frozenset[str]) -> dict:
    # Чужой хост получил бы сам текст команды, а его имя ушло бы DNS-запросом —
    # без подтверждения подключаемся только к хостам владельца.
    if not is_allowed(host.rpartition("@")[2]):
        return {"host": host, "command": command,
                "error": refusal_reason(host, "ssh_exec с подтверждением")}
    if not is_read_only(command, binaries):
        # Отказ теперь бывает двух видов: команда меняет состояние либо её бинарника
        # нет в скоупе этого агента. Перечисляем скоуп, чтобы он не эскалировал
        # читающую команду в ssh_exec с подтверждением на ровном месте.
        return {"host": host, **refusal(command, binaries, "ssh_exec")}
    return {"host": host, **await shell_exec(_ssh_argv(host, command))}


async def ssh_exec(host: str, command: list[str]) -> dict:
    return {"host": host, **await shell_exec(_ssh_argv(host, command))}


def build_access_tools(access: HostAccess) -> list[Tool]:
    binaries = _node_binaries(access)

    async def query(host: str, command: list[str]) -> dict:
        return await ssh_query(host, command, binaries)

    return [
        Tool("ssh_query", "Run ONE READ-ONLY command argv on a REMOTE node over SSH. Allowed binaries: "
             f"{', '.join(sorted(binaries))}. No shell: `sh -c`, pipes and redirects are refused — "
             "make several calls instead. Safe, auto-executed.",
             SshParams, query, Safety.SAFE),
        Tool("ssh_exec", "Run any command on a REMOTE node over SSH (DESTRUCTIVE: restarts, updates, compose). Requires user confirmation.", SshParams, ssh_exec, Safety.DANGEROUS),
    ]
