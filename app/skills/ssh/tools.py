"""SSH как транспорт: один инструмент — одна задача (доставить команду на ноду).

Классификация та же, что для локального хоста: команда на удалённой ноде опасна
ровно так же. Отличие одно — на ноде нет docker-сокета, поэтому read-only
подкоманды `docker` доступны здесь и только здесь.
"""
import shlex

from pydantic import BaseModel, Field

from app.config import settings
from app.skills.host.tools import ACCESS as _HOST_ACCESS
from app.skills.readonly import is_read_only
from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec

_NODE_BINARIES = _HOST_ACCESS.binaries | {"docker"}


def _is_read_only(command: list[str]) -> bool:
    return is_read_only(command, _NODE_BINARIES)


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


async def ssh_query(host: str, command: list[str]) -> dict:
    if not _is_read_only(command):
        return {
            "host": host,
            "command": command,
            "error": "команда не входит в список read-only; для изменяющих операций используй ssh_exec (с подтверждением)",
        }
    return {"host": host, **await shell_exec(_ssh_argv(host, command))}


async def ssh_exec(host: str, command: list[str]) -> dict:
    return {"host": host, **await shell_exec(_ssh_argv(host, command))}


def build_tools() -> list[Tool]:
    return [
        Tool("ssh_query", "Run a READ-ONLY command on a REMOTE node over SSH (docker ps/logs, systemctl status, journalctl, df, free, uptime, ss). May be wrapped in `sh -c '<pipeline>'`. Safe, auto-executed.", SshParams, ssh_query, Safety.SAFE),
        Tool("ssh_exec", "Run any command on a REMOTE node over SSH (DESTRUCTIVE: restarts, updates, compose). Requires user confirmation.", SshParams, ssh_exec, Safety.DANGEROUS),
    ]
