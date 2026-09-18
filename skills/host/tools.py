"""Хост: файлы и конфиги, сервисы, firewall, диски, git. Инструменты собираются из объявленного доступа."""
from app.skills.readonly import HostAccess

ACCESS = HostAccess(
    binaries=frozenset({
        "df", "du", "free", "uptime", "uname", "hostname", "date", "id", "nproc", "echo",
        "lsblk", "lscpu", "ps", "who", "ss", "cat", "ls", "getent",
        "crontab", "iptables", "ip6tables", "ip", "systemctl", "journalctl",
        "head", "tail", "grep", "find", "stat", "readlink", "wc", "nginx", "git",
    }),
    exec_allowed=True,
)
