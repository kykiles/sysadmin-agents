"""SSH как транспорт: один инструмент — одна задача (доставить команду на ноду).

Классификация та же, что для локального хоста: команда на удалённой ноде опасна
ровно так же. Отличие одно — на ноде нет docker-сокета, поэтому read-only
подкоманды `docker` доступны здесь и только здесь.
"""
import shlex

from pydantic import BaseModel, Field

from app.config import settings
from skills.host.tools import ACCESS as _HOST_ACCESS
from app.skills.readonly import HostAccess, is_read_only, refusal
from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec


def _node_binaries(access: HostAccess) -> frozenset[str]:
    """Что читаем на ноде: базовый набор хоста плюс то, что принесли остальные
    выданные агенту скилы (observe даёт top/vmstat, tls — openssl). Иначе одна и
    та же команда проходила локально и отвергалась на ноде."""
    return _HOST_ACCESS.binaries | access.binaries | {"docker"}


class SshParams(BaseModel):
    host: str = Field(description="node IP or hostname (may be user@host)")
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
        target,
        " ".join(shlex.quote(a) for a in command),
    ]


async def ssh_query(host: str, command: list[str], binaries: frozenset[str]) -> dict:
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
        Tool("ssh_query", "Run a READ-ONLY command on a REMOTE node over SSH. Allowed binaries: "
             f"{', '.join(sorted(binaries))}. May be wrapped in `sh -c '<pipeline>'`. Safe, auto-executed.",
             SshParams, query, Safety.SAFE),
        Tool("ssh_exec", "Run any command on a REMOTE node over SSH (DESTRUCTIVE: restarts, updates, compose). Requires user confirmation.", SshParams, ssh_exec, Safety.DANGEROUS),
    ]
