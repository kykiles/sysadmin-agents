from app.tools.base import Tool, Safety
from app.tools.docker import ShellParams, host_exec
# tls_check уже реализован в security-скиле — переиспользуем, чтобы не дублировать
# логику построения openssl-команды и валидацию эндпоинта.
from app.skills.security.tools import tls_check, TlsParams


# Бинарники осмотра сертификатов, безопасные при любых аргументах (только чтение).
_READ_ONLY_BINARIES = {"openssl", "ls", "stat", "cat", "find", "readlink", "test"}

_SYSTEMCTL_READONLY = {
    "status", "show", "is-active", "is-enabled", "is-failed",
    "list-units", "list-timers",
}


def _is_read_only(command: list[str]) -> bool:
    if not command:
        return False
    binary, args = command[0], command[1:]
    if binary in _READ_ONLY_BINARIES:
        return True
    if binary == "crontab":
        # только просмотр текущего crontab
        return args[:1] == ["-l"]
    if binary == "systemctl":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] in _SYSTEMCTL_READONLY
    if binary == "certbot":
        # `certbot certificates` — листинг сертификатов, ничего не меняет
        subs = [a for a in args if not a.startswith("-")]
        return subs[:1] == ["certificates"]
    return False


async def tls_query(command: list[str]) -> dict:
    if not _is_read_only(command):
        return {
            "command": command,
            "error": "команда не входит в список read-only для осмотра сертификатов "
                     "(openssl, ls/stat/cat/find/readlink, crontab -l, "
                     "systemctl list-timers/status, certbot certificates). "
                     "Для продления/reload используй tls_renew (с подтверждением).",
        }
    return await host_exec(command)


def build_tools() -> list[Tool]:
    return [
        Tool("tls_query", "Run a READ-ONLY certificate inspection command on the HOST via nsenter (openssl x509/s_client, ls/stat/cat/find/readlink, crontab -l, systemctl list-timers/status, certbot certificates). Safe, auto-executed. Use to discover how certs are managed and check their state.", ShellParams, tls_query, Safety.SAFE),
        Tool("tls_check", "Check the TLS certificate of an endpoint (host:port): subject, issuer and validity dates. Safe, read-only.", TlsParams, tls_check, Safety.SAFE),
        Tool("tls_renew", "Run a certificate renewal / web-server reload command on the HOST (DESTRUCTIVE): certbot renew, acme.sh --renew, systemctl reload nginx, docker exec <c> nginx -s reload, etc. Requires user confirmation.", ShellParams, host_exec, Safety.DANGEROUS),
    ]
