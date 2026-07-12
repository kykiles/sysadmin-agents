import re
import shlex

from pydantic import BaseModel, Field

from app.tools.base import Tool, Safety
from app.tools.docker import ShellParams, host_exec, host_shell


# Бинарники аудита, безопасные при любых аргументах (только чтение состояния).
_READ_ONLY_BINARIES = {"ss", "ls", "stat", "getent", "lastlog", "who", "w", "id", "sshd"}

_SYSTEMCTL_READONLY = {"status", "show", "is-active", "is-enabled", "is-failed", "list-units"}

_APT_READONLY = {"list", "-s", "--simulate", "--dry-run"}


def _is_read_only(command: list[str]) -> bool:
    if not command:
        return False
    binary, args = command[0], command[1:]
    if binary in _READ_ONLY_BINARIES:
        # sshd допускаем только для дампа эффективного конфига: `sshd -T`
        if binary == "sshd":
            return "-T" in args
        return True
    if binary in ("iptables", "ip6tables"):
        return any(a in ("-L", "--list", "-S", "--list-rules") for a in args) and not any(
            a in ("-A", "-I", "-D", "-R", "-F", "-X", "-P", "-N", "-Z", "-E") for a in args)
    if binary == "ufw":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] == "status"
    if binary == "fail2ban-client":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] in ("status", "get", "ping")
    if binary == "systemctl":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] in _SYSTEMCTL_READONLY
    if binary in ("apt", "apt-get"):
        # только листинг обновлений или симуляция
        if binary == "apt" and args[:1] == ["list"]:
            return True
        return any(a in _APT_READONLY for a in args)
    return False


async def sec_query(command: list[str]) -> dict:
    if not _is_read_only(command):
        return {
            "command": command,
            "error": "команда не входит в список read-only для аудита "
                     "(ss, iptables -L/-S, ufw status, fail2ban-client status, sshd -T, "
                     "apt list --upgradable / apt-get -s upgrade, systemctl status, ls/stat). "
                     "Применение изменений — не твоя задача, отдай их hostadmin.",
        }
    return await host_exec(command)


class TlsParams(BaseModel):
    endpoint: str = Field(description="хост:порт для проверки TLS, например example.com:443")


_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9.\-]+:\d{1,5}$")


async def tls_check(endpoint: str) -> dict:
    if not _ENDPOINT_RE.match(endpoint):
        return {"endpoint": endpoint, "error": "ожидается host:port, например example.com:443"}
    host, _, port = endpoint.rpartition(":")
    q = shlex.quote(endpoint)
    servername = shlex.quote(host)
    script = (
        f"echo | openssl s_client -connect {q} -servername {servername} 2>/dev/null "
        f"| openssl x509 -noout -subject -issuer -dates"
    )
    res = await host_shell(script)
    return {
        "endpoint": endpoint,
        "returncode": res.get("returncode"),
        "certificate": (res.get("stdout") or "").strip(),
        "stderr": res.get("stderr"),
    }


def build_tools() -> list[Tool]:
    return [
        Tool("sec_query", "Run a READ-ONLY security audit command on the HOST via nsenter (ss, iptables -L/-S, ufw status, fail2ban-client status, sshd -T, apt list --upgradable / apt-get -s upgrade, systemctl status, ls/stat). Safe, auto-executed.", ShellParams, sec_query, Safety.SAFE),
        Tool("tls_check", "Check the TLS certificate of an endpoint (host:port): subject, issuer and validity dates. Safe, read-only.", TlsParams, tls_check, Safety.SAFE),
    ]
