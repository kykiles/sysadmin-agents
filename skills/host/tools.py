"""Хост: firewall, systemd, диски. Инструменты собираются из объявленного доступа."""
from app.skills.readonly import HostAccess

ACCESS = HostAccess(
    binaries=frozenset({
        "df", "du", "free", "uptime", "uname", "hostname", "date", "id", "nproc", "echo",
        "lsblk", "lscpu", "ps", "who", "ss", "cat", "ls", "getent",
        "crontab", "iptables", "ip6tables", "ip", "systemctl", "journalctl",
    }),
    exec_allowed=True,
)
