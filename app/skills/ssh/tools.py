"""SSH как транспорт: один инструмент — одна задача (доставить команду на ноду).

Классификацию read-only не дублируем — берём ту же, что у скила host: команда на
удалённой ноде опасна ровно так же, как на локальном хосте.
"""
import shlex

from pydantic import BaseModel, Field

from app.config import settings
from app.skills.host.tools import _is_simple_read_only as _host_read_only
from app.skills.shellsafe import check_wrapped_readonly
from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec

# На ноде docker доступен только через ssh (сокета у нас нет), поэтому read-only
# подкоманды docker разрешаем здесь — в скиле host их нет и быть не должно.
_DOCKER_READONLY = {"ps", "logs", "inspect", "stats", "images", "version", "info", "top", "port", "diff"}


def _is_read_only(command: list[str]) -> bool:
    wrapped = check_wrapped_readonly(command, _is_simple_read_only)
    if wrapped is not None:
        return wrapped
    return _is_simple_read_only(command)


def _is_simple_read_only(command: list[str]) -> bool:
    if command and command[0] == "docker":
        subs = [a for a in command[1:] if not a.startswith("-")]
        if subs and subs[0] == "compose":
            subs = subs[1:]
        return bool(subs) and subs[0] in _DOCKER_READONLY
    return _host_read_only(command)


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
