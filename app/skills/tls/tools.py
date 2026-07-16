from app.tools.base import Tool, Safety
from app.tools.docker import ShellParams, NoParams, host_exec, host_shell
from app.skills.shellsafe import check_wrapped_readonly
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
    wrapped = check_wrapped_readonly(command, _is_simple_read_only)
    if wrapped is not None:
        return wrapped
    return _is_simple_read_only(command)


def _is_simple_read_only(command: list[str]) -> bool:
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


# Один детерминированный проход discovery из плейбука SKILL.md: механизм продления
# (таймеры/certbot/cron/acme.sh), сертификаты на диске и их сроки. Только чтение.
_REPORT_SCRIPT = r"""
echo "## systemd timers (cert/acme)"
systemctl list-timers --all 2>/dev/null | grep -iE 'cert|acme' || echo "(нет таймеров cert/acme)"
echo
echo "## certbot"
if command -v certbot >/dev/null 2>&1; then certbot certificates 2>&1; else echo "(certbot не установлен)"; fi
echo
echo "## cron (root, cert/acme/renew)"
crontab -l 2>/dev/null | grep -iE 'cert|acme|renew' || echo "(нет cron cert/acme)"
echo
echo "## acme.sh"
ls -la /root/.acme.sh 2>/dev/null | head -20 || echo "(нет /root/.acme.sh)"
echo
echo "## /etc/letsencrypt/live"
ls -la /etc/letsencrypt/live/ 2>/dev/null || echo "(нет /etc/letsencrypt/live)"
echo
echo "## сроки сертификатов на диске"
for f in /etc/letsencrypt/live/*/fullchain.pem /opt/*/nginx/*fullchain*.pem /opt/*/certs/*fullchain*.pem /etc/ssl/*/fullchain.pem; do
  [ -f "$f" ] || continue
  echo "=== $f ==="
  openssl x509 -in "$f" -noout -subject -issuer -dates 2>/dev/null || echo "(не удалось прочитать)"
done
"""


async def tls_report() -> dict:
    return await host_shell(_REPORT_SCRIPT)


def build_tools() -> list[Tool]:
    return [
        Tool("tls_report", "One-shot READ-ONLY certificate discovery on the HOST: renewal mechanism (systemd timers, certbot certificates, root cron, acme.sh), on-disk certs under /etc/letsencrypt, /opt/*/nginx, /opt/*/certs, /etc/ssl and their subject/issuer/expiry dates. Safe, auto-executed, no args. Use this FIRST for any 'check certificates' request — it returns the whole picture in a single call.", NoParams, tls_report, Safety.SAFE),
        Tool("tls_query", "Run a READ-ONLY certificate inspection command on the HOST via nsenter (openssl x509/s_client, ls/stat/cat/find/readlink, crontab -l, systemctl list-timers/status, certbot certificates). May be wrapped in `sh -c '<pipeline>'` for pipes/grep/loops — still auto-executed if every command inside is read-only. Safe, auto-executed. Use for targeted follow-ups after tls_report.", ShellParams, tls_query, Safety.SAFE),
        Tool("tls_check", "Check the TLS certificate of an endpoint (host:port): subject, issuer and validity dates. Safe, read-only.", TlsParams, tls_check, Safety.SAFE),
        Tool("tls_renew", "Run a certificate renewal / web-server reload command on the HOST (DESTRUCTIVE): certbot renew, acme.sh --renew, systemctl reload nginx, docker exec <c> nginx -s reload, etc. Requires user confirmation.", ShellParams, host_exec, Safety.DANGEROUS),
    ]
