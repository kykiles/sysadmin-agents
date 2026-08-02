"""Read-only запросы к БД в контейнерах.

Под инструментом лежит тот же `docker_exec`, что у скила `docker` объявлен
DANGEROUS. Разница только в проверке: сюда пропускаем клиент БД с запросом,
в котором нет записывающих глаголов и нет побега в shell/файлы. Всё остальное
уходит на подтверждение через `docker_exec` скила `docker`.
"""
import re

from app.tools.base import Tool, Safety
from app.tools.docker import docker_exec, ExecParams

_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3"}

# Флаги, после которых идёт SQL-запрос.
_QUERY_FLAGS = {"-c", "--command", "-e", "--execute"}

# Флаги, исполняющие произвольный файл: содержимое мы не видим — отказ.
_FILE_FLAGS = {"-f", "--file", "--init", "-init"}

# Записывающие SQL-глаголы. Ищем как отдельные слова в любом месте запроса:
# подзапрос и CTE тоже могут менять данные.
_WRITE_SQL = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|replace|merge|"
    r"grant|revoke|copy|vacuum|reindex|attach|detach|call|do|load|import)\b",
    re.IGNORECASE,
)

# Побег из клиента БД: shell psql (\!), мета-команды записи sqlite3.
_ESCAPES = re.compile(
    r"(\\!|\.shell|\.system|\.output|\.import|\.backup|\.restore)", re.IGNORECASE
)

# Позиционный аргумент считаем запросом, только если он начинается как запрос
# или мета-команда: у sqlite3 запрос идёт последним словом без флага.
_QUERY_START = re.compile(r"^\s*(select|with|show|explain|describe|desc|pragma|\\|\.)", re.IGNORECASE)


def _queries(args: list[str]) -> list[str] | None:
    """Собрать SQL-тексты из argv. None — если argv исполняет файл или запроса нет."""
    found: list[str] = []
    expect = False
    for a in args:
        if expect:
            found.append(a)
            expect = False
            continue
        if a in _FILE_FLAGS:
            return None
        if a in _QUERY_FLAGS:
            expect = True
            continue
        if not a.startswith("-") and _QUERY_START.match(a):
            found.append(a)
    if expect or not found:
        return None
    return found


def _is_read_only(command: list[str]) -> bool:
    if not command or command[0] not in _CLIENTS:
        return False
    qs = _queries(command[1:])
    if qs is None:
        return False
    return not any(_WRITE_SQL.search(q) or _ESCAPES.search(q) for q in qs)


async def docker_query(container: str, command: list[str]) -> dict:
    if not _is_read_only(command):
        return {
            "container": container,
            "command": command,
            "error": "пропускаются только psql/mysql/mariadb/sqlite3 с явным запросом "
                     "(-c/-e или позиционный SELECT), без записывающих глаголов "
                     "(INSERT/UPDATE/DELETE/DDL/COPY) и без побега в shell. "
                     "Изменяющие запросы — через docker_exec (с подтверждением).",
        }
    return await docker_exec(container, command)


def build_tools() -> list[Tool]:
    return [
        Tool("docker_query", "Run a READ-ONLY database query inside a container (psql, mysql, sqlite3) passed via -c/-e. Refuses writes, DDL and shell escapes. Safe, auto-executed. For anything that modifies data use docker_exec (requires confirmation).", ExecParams, docker_query, Safety.SAFE),
    ]
