from app.tools.base import Tool, Safety
from app.tools.docker import (
    ShellParams, host_exec,
    docker_ps, docker_logs, docker_stats,
    NoParams, ContainerParams, LogsParams,
)


# Бинарники, безопасные для диагностики при любых аргументах.
_READ_ONLY_BINARIES = {
    "free", "uptime", "vmstat", "iostat", "mpstat", "df", "du",
    "ps", "top", "dmesg", "who", "w", "uname", "hostname", "lsof",
}

# Read-only подкоманды systemctl (совпадает с host-skill).
_SYSTEMCTL_READONLY = {
    "status", "show", "cat", "is-active", "is-enabled", "is-failed",
    "list-units", "list-unit-files", "list-timers", "list-sockets", "get-default",
}

# tail/cat разрешены только для чтения журналов под /var/log.
_LOG_READERS = {"tail", "cat", "head", "less", "zcat", "grep", "egrep", "wc"}


def _is_read_only(command: list[str]) -> bool:
    if not command:
        return False
    binary, args = command[0], command[1:]
    if binary in _READ_ONLY_BINARIES:
        return True
    if binary == "ss":
        return True
    if binary == "ip":
        # только просмотр: без изменяющих подкоманд
        return not any(a in {"add", "del", "delete", "set", "change", "replace", "flush"} for a in args)
    if binary == "journalctl":
        return not any(a in {"--rotate", "--vacuum-size", "--vacuum-time", "--vacuum-files", "--flush", "--sync"} for a in args)
    if binary == "systemctl":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] in _SYSTEMCTL_READONLY
    if binary in _LOG_READERS:
        # разрешаем только чтение абсолютных путей под /var/log;
        # опции и их значения (напр. -n 100) игнорируем
        paths = [a for a in args if a.startswith("/")]
        return bool(paths) and all(p.startswith("/var/log") for p in paths)
    return False


async def observe_query(command: list[str]) -> dict:
    if not _is_read_only(command):
        return {
            "command": command,
            "error": "команда не входит в список read-only для диагностики "
                     "(journalctl, ss, free, vmstat, iostat, df, du, ps, top, dmesg, "
                     "ip show, systemctl status, tail/cat под /var/log)",
        }
    return await host_exec(command)


def build_tools() -> list[Tool]:
    return [
        Tool("observe_query", "Run a READ-ONLY diagnostic command on the HOST via nsenter (journalctl, ss, free, vmstat, iostat, df, du, ps, top, dmesg, ip show, systemctl status, tail/cat under /var/log). Safe, auto-executed.", ShellParams, observe_query, Safety.SAFE),
        Tool("docker_ps", "List all containers with state/status/ports (read-only).", NoParams, docker_ps, Safety.SAFE),
        Tool("docker_logs", "Read trailing logs of a container (read-only).", LogsParams, docker_logs, Safety.SAFE),
        Tool("docker_stats", "Read live cpu/memory/pids stats of a container (read-only).", ContainerParams, docker_stats, Safety.SAFE),
    ]
