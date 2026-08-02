"""Единая проверка «команда только читает» и объявление доступа к хосту.

Раньше эта логика существовала в пяти копиях — по одной у скилов host, observe,
tls, security и ssh, — и копии разошлись: список read-only подкоманд systemctl
насчитывал то десять вариантов, то пять. Здесь она одна.

Скил не переопределяет логику, а объявляет `ACCESS = HostAccess(...)`: какие
бинарники ему вообще доступны и можно ли ему выполнять изменяющие команды.
Классификация аргументов (какие подкоманды `systemctl` читают, а какие меняют)
универсальна — она зависит от команды, а не от того, кто её вызвал.

При спавне доступы выданных скилов объединяются в один инструмент `host_query`,
поэтому имя у него одно и коллизии имён с разным скоупом невозможны.
"""
from dataclasses import dataclass, field
from typing import Callable

from app.skills.shellsafe import check_wrapped_readonly
from app.tools.base import Tool, Safety
from app.tools.docker import ShellParams, host_exec


@dataclass(frozen=True)
class HostAccess:
    """Что скил разрешает делать на хосте."""

    binaries: frozenset[str] = field(default_factory=frozenset)
    # можно ли выдавать инструмент изменяющих команд (с подтверждением)
    exec_allowed: bool = False

    def __or__(self, other: "HostAccess") -> "HostAccess":
        return HostAccess(
            binaries=self.binaries | other.binaries,
            exec_allowed=self.exec_allowed or other.exec_allowed,
        )


# ---------- бинарники, безопасные при любых аргументах ----------

_ALWAYS_SAFE = frozenset({
    # состояние системы
    "df", "du", "free", "uptime", "uname", "hostname", "date", "id",
    "lsblk", "lscpu", "vmstat", "iostat", "mpstat", "ps", "top", "dmesg",
    "who", "w", "lsof", "ss", "getent", "lastlog",
    # файлы и текст: чтение не меняет состояние
    "ls", "stat", "cat", "head", "tail", "zcat", "grep", "egrep", "wc",
    "find", "readlink", "test", "openssl",
})


# ---------- бинарники, у которых читающими являются только часть вызовов ----------

_SYSTEMCTL_READONLY = frozenset({
    "status", "show", "cat", "is-active", "is-enabled", "is-failed",
    "list-units", "list-unit-files", "list-timers", "list-sockets", "get-default",
})

_IPTABLES_LISTING = frozenset({"-L", "--list", "-S", "--list-rules"})
_IPTABLES_MUTATING = frozenset({
    "-A", "--append", "-I", "--insert", "-D", "--delete", "-R", "--replace",
    "-F", "--flush", "-X", "--delete-chain", "-P", "--policy", "-N", "--new-chain",
    "-Z", "--zero", "-E", "--rename-chain",
})

_IP_MUTATING = frozenset({"add", "del", "delete", "set", "change", "replace", "flush"})

_JOURNALCTL_MUTATING = frozenset({
    "--rotate", "--vacuum-size", "--vacuum-time", "--vacuum-files", "--flush", "--sync",
})

_APT_READONLY = frozenset({"-s", "--simulate", "--dry-run"})

# На ноде docker доступен только через ssh, сокета у нас там нет.
_DOCKER_READONLY = frozenset({
    "ps", "logs", "inspect", "stats", "images", "version", "info", "top", "port", "diff",
})


def _subcommands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _systemctl(args: list[str]) -> bool:
    subs = _subcommands(args)
    return bool(subs) and subs[0] in _SYSTEMCTL_READONLY


def _iptables(args: list[str]) -> bool:
    if any(a in _IPTABLES_MUTATING for a in args):
        return False
    return any(a in _IPTABLES_LISTING for a in args)


def _apt(args: list[str]) -> bool:
    subs = _subcommands(args)
    return subs[:1] == ["list"] or any(a in _APT_READONLY for a in args)


def _docker(args: list[str]) -> bool:
    subs = _subcommands(args)
    if subs[:1] == ["compose"]:
        subs = subs[1:]
    return bool(subs) and subs[0] in _DOCKER_READONLY


_CHECKS: dict[str, Callable[[list[str]], bool]] = {
    "crontab": lambda args: args[:1] == ["-l"],
    "iptables": _iptables,
    "ip6tables": _iptables,
    "ip": lambda args: not any(a in _IP_MUTATING for a in args),
    "systemctl": _systemctl,
    "journalctl": lambda args: not any(a in _JOURNALCTL_MUTATING for a in args),
    "ufw": lambda args: _subcommands(args)[:1] == ["status"],
    "fail2ban-client": lambda args: _subcommands(args)[:1] in (["status"], ["get"], ["ping"]),
    "sshd": lambda args: "-T" in args,  # только дамп эффективного конфига
    "apt": _apt,
    "apt-get": _apt,
    "certbot": lambda args: _subcommands(args)[:1] == ["certificates"],
    "docker": _docker,
}

# Всё, что вообще может быть признано читающим. Скоуп скила — подмножество отсюда.
KNOWN_BINARIES = _ALWAYS_SAFE | frozenset(_CHECKS)


def is_read_only(command: list[str], binaries: frozenset[str]) -> bool:
    """Читает ли команда, не меняя состояния, в пределах разрешённых бинарников.

    `sh -c '<pipeline>'` разбирается на простые команды: читающим считается
    только пайплайн, где каждая команда проходит ту же проверку.
    """
    wrapped = check_wrapped_readonly(command, lambda c: _simple(c, binaries))
    if wrapped is not None:
        return wrapped
    return _simple(command, binaries)


def _simple(command: list[str], binaries: frozenset[str]) -> bool:
    if not command:
        return False
    binary, args = command[0], command[1:]
    if binary not in binaries:
        return False
    if binary in _ALWAYS_SAFE:
        return True
    check = _CHECKS.get(binary)
    return check is not None and check(args)


def refusal(command: list[str], binaries: frozenset[str]) -> dict:
    return {
        "command": command,
        "error": "команда не входит в список read-only для этого агента "
                 f"(доступны: {', '.join(sorted(binaries))}). "
                 "Для изменяющих операций используй shell_exec (с подтверждением).",
    }


def build_host_tools(access: HostAccess) -> list[Tool]:
    """Инструменты доступа к хосту по объединённому доступу выданных скилов."""
    if not access.binaries and not access.exec_allowed:
        return []

    async def host_query(command: list[str]) -> dict:
        if not is_read_only(command, access.binaries):
            return refusal(command, access.binaries)
        return await host_exec(command)

    tools = []
    if access.binaries:
        allowed = ", ".join(sorted(access.binaries))
        tools.append(Tool(
            "host_query",
            "Run a READ-ONLY command on the HOST via nsenter. Allowed binaries: "
            f"{allowed}. May be wrapped in `sh -c '<pipeline>'` for pipes/grep/loops — "
            "still auto-executed if every command inside is read-only. "
            "Safe, auto-executed. For anything that changes state use shell_exec.",
            ShellParams, host_query, Safety.SAFE,
        ))
    if access.exec_allowed:
        tools.append(Tool(
            "shell_exec",
            "Run a state-changing command on the HOST via nsenter (DESTRUCTIVE): "
            "service restarts, certificate renewal, firewall changes, package installs. "
            "Requires user confirmation.",
            ShellParams, host_exec, Safety.DANGEROUS,
        ))
    return tools
