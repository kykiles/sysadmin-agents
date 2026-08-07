"""Аудит безопасности: порты, firewall, fail2ban, конфиг sshd, обновления."""
import re
import shlex

from pydantic import BaseModel, Field

from app.skills.readonly import HostAccess
from app.tools.base import Tool, Safety
from app.tools.docker import host_shell

ACCESS = HostAccess(binaries=frozenset({
    "ss", "ls", "stat", "getent", "lastlog", "who", "w", "id", "sshd",
    "iptables", "ip6tables", "ufw", "fail2ban-client", "systemctl",
    "apt", "apt-get",
}))


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
        Tool("tls_check", "Check the TLS certificate of an endpoint (host:port): subject, issuer and validity dates. Safe, read-only.", TlsParams, tls_check, Safety.SAFE),
    ]
