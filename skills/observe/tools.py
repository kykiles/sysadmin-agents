"""Диагностика: нагрузка, журналы, состояние контейнеров.

Чтение файлов больше не ограничено каталогом `/var/log`: ограничение защищало от
раскрытия, а не от изменения, и всё равно снималось, если агенту выдавали заодно
скил `host` или `tls` — там `cat` был доступен без всяких путей.
"""
from app.skills.readonly import HostAccess
from app.tools.base import Tool, Safety
from app.tools.docker import (
    docker_ps, docker_logs, docker_stats,
    NoParams, ContainerParams, LogsParams,
)

ACCESS = HostAccess(binaries=frozenset({
    "free", "uptime", "vmstat", "iostat", "mpstat", "df", "du", "nproc", "echo",
    "ps", "top", "dmesg", "who", "w", "uname", "hostname", "lsof",
    "ss", "ip", "journalctl", "systemctl",
    "tail", "cat", "head", "zcat", "grep", "egrep", "wc",
}))


def build_tools() -> list[Tool]:
    return [
        Tool("docker_ps", "List all containers with state/status/ports (read-only).", NoParams, docker_ps, Safety.SAFE),
        Tool("docker_logs", "Read trailing logs of a container (read-only).", LogsParams, docker_logs, Safety.SAFE),
        Tool("docker_stats", "Read live cpu/memory/pids stats of a container (read-only).", ContainerParams, docker_stats, Safety.SAFE),
    ]
