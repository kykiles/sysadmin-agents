from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec, ShellParams


# Бинарники, которые не меняют состояние системы при любых аргументах.
_READ_ONLY_BINARIES = {
    "df", "du", "free", "uptime", "uname", "ss", "cat", "ls",
    "ps", "who", "date", "hostname", "id", "lsblk", "lscpu",
}

# Флаги iptables, изменяющие правила.
_IPTABLES_MUTATING = {
    "-A", "--append", "-I", "--insert", "-D", "--delete", "-R", "--replace",
    "-F", "--flush", "-X", "--delete-chain", "-P", "--policy", "-N", "--new-chain",
    "-Z", "--zero", "-E", "--rename-chain",
}

# Подкоманды ip, изменяющие конфигурацию.
_IP_MUTATING = {"add", "del", "delete", "set", "change", "replace", "flush"}

# Read-only подкоманды systemctl.
_SYSTEMCTL_READONLY = {
    "status", "show", "cat", "is-active", "is-enabled", "is-failed",
    "list-units", "list-unit-files", "list-timers", "list-sockets", "get-default",
}

# Флаги journalctl, изменяющие журнал.
_JOURNALCTL_MUTATING = {"--rotate", "--vacuum-size", "--vacuum-time", "--vacuum-files", "--flush", "--sync"}


def _is_read_only(command: list[str]) -> bool:
    if not command:
        return False
    binary, args = command[0], command[1:]
    if binary in _READ_ONLY_BINARIES:
        return True
    if binary in ("iptables", "ip6tables"):
        if any(a in _IPTABLES_MUTATING for a in args):
            return False
        return any(a in ("-L", "--list", "-S", "--list-rules") for a in args)
    if binary == "ip":
        return not any(a in _IP_MUTATING for a in args)
    if binary == "systemctl":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] in _SYSTEMCTL_READONLY
    if binary == "journalctl":
        return not any(a in _JOURNALCTL_MUTATING for a in args)
    return False


async def host_query(command: list[str]) -> dict:
    if not _is_read_only(command):
        return {
            "command": command,
            "error": "команда не входит в список read-only; для изменяющих операций используй shell_exec (с подтверждением)",
        }
    return await shell_exec(command)


def build_tools() -> list[Tool]:
    return [
        Tool("host_query", "Run a READ-ONLY host command (iptables -L/-S, df, du, ss, ip show, systemctl status, journalctl, free, uptime). Safe, auto-executed. For anything that changes state use shell_exec.", ShellParams, host_query, Safety.SAFE),
        Tool("shell_exec", "Run a shell command on the host system (DESTRUCTIVE). Requires user confirmation.", ShellParams, shell_exec, Safety.DANGEROUS),
    ]
