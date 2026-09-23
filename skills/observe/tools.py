"""Диагностика: нагрузка, журналы, состояние контейнеров, счёт по логам.

Чтение файлов больше не ограничено каталогом `/var/log`: ограничение защищало от
раскрытия, а не от изменения, и всё равно снималось, если агенту выдавали заодно
скил `host` или `tls` — там `cat` был доступен без всяких путей.
"""
import asyncio
import re
from collections import Counter

from pydantic import BaseModel, Field

from app.skills.readonly import HostAccess, is_read_only, refusal
from app.tools.base import Tool, Safety
from app.tools.docker import (
    docker_ps, docker_logs, docker_stats, host_exec,
    NoParams, ContainerParams, LogsParams,
)

ACCESS = HostAccess(binaries=frozenset({
    "free", "uptime", "vmstat", "iostat", "mpstat", "df", "du", "nproc", "echo",
    "ps", "top", "dmesg", "who", "w", "uname", "hostname", "lsof",
    "ss", "ip", "journalctl", "systemctl",
    "tail", "cat", "head", "zcat", "grep", "egrep", "wc",
}))

# Файл читается в память контейнера целиком: на 2 vCPU это потолок, за которым
# счёт стоит дороже ответа. Больше — пусть агент сузит выборку сам.
_MAX_BYTES = 64 * 1024 * 1024
_SAMPLE_CHARS = 300


class Window(BaseModel):
    pattern: str = Field(description=r'regex with ONE capturing group holding a NUMBER, '
                                     r'e.g. "\"ts\":([0-9.]+)" for a unix timestamp')
    min: float = Field(description="lower bound, inclusive")
    max: float = Field(description="upper bound, inclusive")


class LogStatsParams(BaseModel):
    paths: list[str] = Field(description="log files on the host, counted together "
                                         "(current + rotated one when the window spans them)",
                             min_length=1, max_length=5)
    extract: str = Field(description="regex; its group 1 — or the whole match when it has no "
                                     'group — is the value being counted, e.g. "\\"status\\":([0-9]+)"')
    where: str | None = Field(default=None, description="optional regex; only lines matching it count")
    window: Window | None = Field(default=None, description="optional numeric range filter, "
                                                            "e.g. a unix-time window")
    top: int = Field(default=20, ge=1, le=100, description="how many most frequent values to return")


def _refuse_unreadable(paths: list[str]) -> dict | None:
    """Ни байта сверх host_query: тот же `zcat -f`, та же общая классификация.

    Инструмент не расширяет доступ, он избавляет от перебора grep'ом — поэтому
    путь проходит ровно ту проверку, что прошёл бы вызов `zcat -f` руками.
    """
    for path in paths:
        if not is_read_only(_read_argv([path]), ACCESS.binaries):
            return refusal(_read_argv([path]), ACCESS.binaries)
    return None


def _read_argv(paths: list[str]) -> list[str]:
    # `-f` отдаёт несжатый файл как есть: текущий лог и ротированный `.gz` — одним вызовом.
    return ["zcat", "-f", *paths]


def _too_big(paths: list[str], size: int) -> dict:
    return {"files": paths, "bytes": size,
            "error": f"{size // 1024 // 1024} МБ — больше {_MAX_BYTES // 1024 // 1024} МБ; "
                     "возьми файл поменьше или сузь выборку через host_query"}


async def _total_size(paths: list[str]) -> tuple[int, str]:
    res = await host_exec(["stat", "-c", "%s", *paths])
    if res.get("returncode"):
        return 0, (res.get("stderr") or "").strip() or "файл не найден"
    try:
        return sum(int(x) for x in (res.get("stdout") or "").split()), ""
    except ValueError:
        return 0, "не удалось прочитать размер файлов"


def _count(text: str, rx: re.Pattern, rx_where: re.Pattern | None,
           rx_win: re.Pattern | None, lo: float, hi: float, top: int) -> dict:
    counts: Counter[str] = Counter()
    lines = text.splitlines()
    matched = extracted = 0
    sample = ""
    for line in lines:
        if rx_where is not None and not rx_where.search(line):
            continue
        if rx_win is not None:
            m = rx_win.search(line)
            if m is None:
                continue
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if not lo <= value <= hi:
                continue
        matched += 1
        if not sample:
            sample = line[:_SAMPLE_CHARS]
        if (m := rx.search(line)) is not None:
            counts[m.group(1) if rx.groups else m.group(0)] += 1
            extracted += 1
    top_values = counts.most_common(top)
    out = {
        "lines": len(lines),
        "matched": matched,
        "extracted": extracted,
        "distinct": len(counts),
        "top": top_values,
        "other": extracted - sum(c for _, c in top_values),
        # Живая строка в каждом ответе: без неё шаблон чинят вслепую, догадкой по
        # догадке, и одна задача сгорает на сотне вызовов (разбор 21.09.2026).
        "sample": sample or (lines[0][:_SAMPLE_CHARS] if lines else ""),
    }
    if not matched:
        out["note"] = ("ни одна строка не прошла фильтр; `sample` — первая строка файла, "
                       "свери `where`/`window` по ней")
    elif not extracted:
        out["note"] = ("`extract` не совпал ни в одной отобранной строке; `sample` — такая "
                       "строка, свери шаблон по ней")
    return out


async def log_stats(paths: list[str], extract: str, where: str | None = None,
                    window: dict | None = None, top: int = 20) -> dict:
    """Распределение значений по логу: фильтр и счёт делает код, не модель."""
    if (denied := _refuse_unreadable(paths)) is not None:
        return denied
    try:
        rx = re.compile(extract)
        rx_where = re.compile(where) if where else None
        rx_win = re.compile(window["pattern"]) if window else None
    except re.error as e:
        return {"error": f"неверный regex: {e}"}
    if rx_win is not None and rx_win.groups != 1:
        return {"error": "`window.pattern` должен содержать ровно одну группу с числом, "
                         r'например "\"ts\":([0-9.]+)"'}

    size, problem = await _total_size(paths)
    if problem:
        return {"files": paths, "error": problem}
    if size > _MAX_BYTES:
        return _too_big(paths, size)

    res = await host_exec(_read_argv(paths))
    if res.get("returncode"):
        return {"files": paths, "error": (res.get("stderr") or "").strip() or "не прочитать"}
    text = res.get("stdout") or ""
    # `stat` видит сжатый размер: `.gz` лога распаковывается раз в 10–15 больше.
    if len(text) > _MAX_BYTES:
        return _too_big(paths, len(text))
    stats = await asyncio.to_thread(
        _count, text, rx, rx_where, rx_win,
        float(window["min"]) if window else 0.0,
        float(window["max"]) if window else 0.0, top,
    )
    return {"files": paths, "bytes": size, **stats}


def build_tools() -> list[Tool]:
    return [
        Tool("docker_ps", "List all containers with state/status/ports (read-only).", NoParams, docker_ps, Safety.SAFE),
        Tool("docker_logs", "Read trailing logs of a container (read-only).", LogsParams, docker_logs, Safety.SAFE),
        Tool("docker_stats", "Read live cpu/memory/pids stats of a container (read-only).", ContainerParams, docker_stats, Safety.SAFE),
        Tool("log_stats",
             "Count a distribution over log FILES on the host: how many times each value of "
             "`extract` occurs, optionally only on lines matching `where` and inside a numeric "
             "`window` (e.g. a unix-time range). Reading, filtering and counting happen outside "
             "the model — one call replaces a series of greps. Rotated `.gz` archives are read as is. Every answer carries a real `sample` "
             "line: fix your regex against it instead of guessing. Safe, read-only.",
             LogStatsParams, log_stats, Safety.SAFE),
    ]
