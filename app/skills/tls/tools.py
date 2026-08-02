"""Сертификаты: механизм продления, сроки, точечный осмотр.

Продление и reload идут через общий `shell_exec` (DANGEROUS) — отдельного
`tls_renew` больше нет, это была та же команда на хосте под другим именем.
"""
from app.skills.readonly import HostAccess
from app.tools.base import Tool, Safety
from app.tools.docker import NoParams, host_shell
# tls_check реализован в security-скиле — переиспользуем, чтобы не дублировать
# логику построения openssl-команды и валидацию эндпоинта.
from app.skills.security.tools import tls_check, TlsParams

ACCESS = HostAccess(
    binaries=frozenset({
        "openssl", "ls", "stat", "cat", "find", "readlink", "test",
        "crontab", "systemctl", "certbot",
    }),
    exec_allowed=True,
)


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
        Tool("tls_check", "Check the TLS certificate of an endpoint (host:port): subject, issuer and validity dates. Safe, read-only.", TlsParams, tls_check, Safety.SAFE),
    ]
