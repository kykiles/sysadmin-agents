"""Запросы к БД в контейнерах через клиент (psql, mysql, sqlite3).

Под инструментом лежит тот же `docker_exec`, что у скила `docker`. Строковый
фильтр ниже не доказывает read-only: он пропускал `--command=`, `\\i файл`,
`sqlite3 -cmd` и изменяющий PRAGMA (аудит 2026-09-12, F03). Поэтому инструмент
DANGEROUS — каждый вызов подтверждает человек, — а фильтр остался дополнительным
отказом для явно изменяющих форм. Автоматическое чтение вернёт структурированный
интерфейс с ограниченной ролью БД, а не очередное правило в этом фильтре.
"""
import re

from app.tools.base import Tool, Safety
from app.tools.docker import docker_exec, container_missing, ExecParams

_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3"}

# Флаги, после которых идёт SQL-запрос.
_QUERY_FLAGS = {"-c", "--command", "-e", "--execute"}

# Флаги, исполняющие произвольный файл: содержимое мы не видим — отказ.
_FILE_FLAGS = {"-f", "--file", "--init", "-init"}

# psql: перечисление баз, запроса в argv нет, но читающее.
_LISTING_FLAGS = {"-l", "--list"}

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
    args = command[1:]
    qs = _queries(args)
    if qs is None:
        # `psql -l` перечисляет базы, но SQL-текста в argv нет — раньше это был
        # отказ, и агенту приходилось угадывать имя БД вместо того, чтобы спросить.
        return (
            command[0] == "psql"
            and any(a in _LISTING_FLAGS for a in args)
            and not any(a in _FILE_FLAGS for a in args)
        )
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
        Tool("docker_query", "Run a database query inside a container via its client (psql, mysql, sqlite3; query passed via -c/-e). Refuses obvious writes, DDL and shell escapes, but that filter is NOT a read-only guarantee, so EVERY call requires user confirmation — gather what you need in one or two queries. For data changes use docker_exec.", ExecParams, docker_query, Safety.DANGEROUS, precheck=container_missing),
    ]
