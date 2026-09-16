"""Запросы к БД в контейнерах через клиент (psql, mysql, sqlite3).

Под инструментом лежит тот же `docker_exec`, что у скила `docker`. Строковый
фильтр ниже не доказывает read-only: он пропускал `--command=`, `\\i файл`,
`sqlite3 -cmd` и изменяющий PRAGMA (аудит 2026-09-12, F03). Поэтому инструмент
DANGEROUS — каждый вызов подтверждает человек, — а фильтр остался дополнительным
отказом для явно изменяющих форм.

Автоматическое чтение — `pg_read` (T17): argv собирает код, запрос идёт под ролью
`agent_ro` без прав записи (agent_ro.sql) в транзакции READ ONLY. Гарантию даёт
база, а текст проверяется лишь на то, что без проверки её обходит: второй
statement (`;` выводит из транзакции), метакоманду psql и conninfo вместо имени БД
(`-d "user=postgres"` подключает суперпользователем). Всё проверено на postgres:16.
"""
import re

from pydantic import BaseModel, Field

from app.tools.base import Tool, Safety
from app.tools.docker import docker_exec, docker_ps, container_missing, ExecParams, NoParams

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


class PgReadParams(BaseModel):
    container: str = Field(description="имя контейнера PostgreSQL (см. docker_ps)")
    database: str = Field(default="postgres", description="имя базы; список — запрос \\l")
    query: str = Field(description="ОДИН запрос без ';' внутри (SELECT/WITH/EXPLAIN) "
                                   "или одна из метакоманд: \\l, \\dt, \\dn, \\dv, \\d <таблица>")


_PG_ROLE = "agent_ro"
_DB_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")
# Метакоманды psql, которые только описывают схему; аргумент — одно имя объекта.
_PG_DESCRIBE = re.compile(r"\\(l|dt|dn|dv|d\+?)( +[A-Za-z0-9_.]+)?")
# Роль с такими правами обходит READ ONLY (COPY TO PROGRAM не пишет в базу) — отказ.
_PG_GUARD = (
    "DO $$BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND (rolsuper"
    " OR pg_has_role(current_user, 'pg_execute_server_program', 'USAGE')"
    " OR pg_has_role(current_user, 'pg_write_server_files', 'USAGE')"
    " OR pg_has_role(current_user, 'pg_write_all_data', 'USAGE')))"
    " THEN RAISE EXCEPTION 'роль agent_ro имеет права записи — чтение отключено'; END IF; END$$"
)


def _pg_read_argv(database: str, query: str) -> list[str] | str:
    """argv для psql или причина отказа."""
    if not _DB_NAME.fullmatch(database):
        return "database — только имя базы (буквы, цифры, _ . -), без параметров подключения"
    q = query.strip().rstrip(";").strip()
    if not q:
        return "пустой запрос"
    if q.startswith("\\"):
        if not _PG_DESCRIBE.fullmatch(q):
            return "из метакоманд доступны только \\l, \\dt, \\dn, \\dv, \\d <таблица>"
    elif ";" in q:
        return "один запрос за вызов: ';' внутри запроса не принимается — сделай несколько вызовов"
    return [
        "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", _PG_ROLE, "-d", database,
        "-c", _PG_GUARD,
        "-c", "SET statement_timeout = '15s'",
        "-c", "BEGIN TRANSACTION READ ONLY",
        "-c", q,
        "-c", "ROLLBACK",
    ]


async def pg_read(container: str, query: str, database: str = "postgres") -> dict:
    argv = _pg_read_argv(database, query)
    if isinstance(argv, str):
        return {"container": container, "error": argv}
    out = await docker_exec(container, argv)
    output = out.get("output") or ""
    if out.get("exit_code") and f'"{_PG_ROLE}"' in output:
        out["hint"] = (f"в этом контейнере не настроена роль {_PG_ROLE} (skills/db/agent_ro.sql); "
                       "прочитать можно через docker_query — с подтверждением")
    # argv со служебными -c агенту ни к чему: он видит свой запрос и вывод.
    out["command"] = ["pg_read", database, query]
    return out


def build_tools() -> list[Tool]:
    return [
        Tool("docker_ps", "List all docker containers — find the database container here instead of guessing its name.", NoParams, docker_ps, Safety.SAFE),
        Tool("pg_read", "Read from a PostgreSQL database in a container. Safe, runs without confirmation: read-only role in a READ ONLY transaction. ONE statement per call (no ';' inside), or a describe meta-command (\\l, \\dt, \\dn, \\dv, \\d table). Prefer this over docker_query for any reading.", PgReadParams, pg_read, Safety.SAFE),
        Tool("docker_query", "Run a database query inside a container via its client (psql, mysql, sqlite3; query passed via -c/-e). Refuses obvious writes, DDL and shell escapes, but that filter is NOT a read-only guarantee, so EVERY call requires user confirmation — gather what you need in one or two queries. For data changes use docker_exec.", ExecParams, docker_query, Safety.DANGEROUS, precheck=container_missing),
    ]
