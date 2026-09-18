"""Мост к MCP: чужой сервер становится набором инструментов обычного скилла.

Скилл объявляет сервер во frontmatter (`mcp: {url: ...}`) — питоновский `tools.py`
ему не нужен. Дальше всё работает как со своими инструментами: айсберг, spawn,
объединение доступов.

Соединение открывается на каждый вызов и сразу закрывается: сессия MCP живёт в
контекстном менеджере, а держать её между задачами — это чинить переподключения,
таймауты и протухшие сессии.
ponytail: одно лишнее рукопожатие на вызов; долгоживущая сессия, если задержка станет
заметной.
"""
import asyncio
import os
import re
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, model_validator

from app.logging import get_logger, register_secret
from app.tools.base import Safety, Tool

log = get_logger("mcp")

TIMEOUT = 30
# ponytail: одна константа на все серверы; вынести в frontmatter, если появится
# сервер, которому нужен другой потолок
MAX_RESULT_CHARS = 6000


class AnyParams(BaseModel):
    """Аргументы описывает сервер своей JSON-схемой — поля берём как есть, а
    проверяет их модель из `_params_model`."""

    model_config = ConfigDict(extra="allow")


def _params_model(schema: dict) -> type[BaseModel]:
    """Модель параметров, проверяющая аргументы объявленной сервером JSON Schema —
    настоящим валидатором, до подтверждения и вызова. Раньше схема шла только в
    описание для модели, а лишние поля доходили до сервера (аудит 2026-09-12, F07)."""
    validator = validator_for(schema)(schema)

    class Params(AnyParams):
        @model_validator(mode="before")
        @classmethod
        def _check(cls, data):
            error = best_match(validator.iter_errors(data))
            if error is not None:
                where = "/".join(str(p) for p in error.absolute_path) or "аргументы"
                raise ValueError(f"{where}: {error.message}")
            return data

    return Params


# Конструкции, замкнутость которых здесь не проверить: такая схема считается открытой.
_UNSUPPORTED = frozenset({
    "$ref", "$defs", "definitions", "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    "patternProperties", "dependentSchemas", "unevaluatedProperties", "additionalItems",
    "prefixItems", "contains",
})


def _closed(schema) -> bool:
    """Закрыт ли контракт: у каждого значения задан тип, у каждого объекта
    `additionalProperties: false`, у массива — закрытые items. Открытую схему
    SAFE не объявляем: непроверенные поля ушли бы на сервер без подтверждения."""
    if not isinstance(schema, dict) or _UNSUPPORTED & schema.keys():
        return False
    if not {"type", "enum", "const"} & schema.keys():
        return False
    types = schema.get("type")
    types = set(types) if isinstance(types, list) else {types}
    if "object" in types:
        if schema.get("additionalProperties") is not False:
            return False
        if not all(_closed(s) for s in schema.get("properties", {}).values()):
            return False
    if "array" in types and not _closed(schema.get("items")):
        return False
    return True


def _remote_tool(url: str, server_id: str, spec, safety: Safety, skill_name: str) -> Tool | None:
    """Инструмент одного метода сервера. Имя метода захвачено замыканием вне
    аргументов вызова: `fn(_name=spec.name, **kwargs)` позволял аргументом `_name`
    вызвать на сервере другой метод, чем объявлен, показан и записан в журнал."""
    schema = spec.input_schema
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as e:
        log.warning("mcp_schema_invalid", skill=skill_name, tool=spec.name, error=e.message)
        return None
    if safety is Safety.SAFE and not _closed(schema):
        log.warning("mcp_schema_open", skill=skill_name, tool=spec.name,
                    note="схема не закрыта — вызов только с подтверждением")
        safety = Safety.DANGEROUS
    remote = spec.name

    async def fn(**kwargs) -> str:
        return await asyncio.wait_for(_call_tool(url, remote, kwargs), TIMEOUT)

    return Tool(
        name=spec.name,
        description=spec.description or spec.name,
        params_model=_params_model(schema),
        fn=fn,
        safety=safety,
        params_schema=schema,
        remote=(server_id, remote),
    )


def _run(coro):
    """Выполнить корутину синхронно, независимо от того, крутится ли уже loop.

    Навыки читаются синхронно и на старте (внутри async main), и после write_skill
    (внутри потока), поэтому asyncio.run напрямую применить нельзя.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 — пробрасываем в вызывающий поток
            box["error"] = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


@asynccontextmanager
async def _session(url: str):
    # Транспорт — streamable HTTP: в образе нет node, а значит и stdio-серверов.
    # Авторизация идёт ключом в URL; заголовки поддержим, когда появится сервер,
    # который иначе не умеет.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _list_tools(url: str) -> list:
    async with _session(url) as session:
        return (await session.list_tools()).tools


async def _call_tool(url: str, name: str, args: dict) -> str:
    async with _session(url) as session:
        result = await session.call_tool(name, args)
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    if not texts:
        return "(пустой ответ)"
    out = "\n".join(texts)
    if len(out) > MAX_RESULT_CHARS:
        # Извлечение страницы возвращает её целиком, и через несколько вызовов
        # контекст агента раздувается так, что каждая следующая генерация модели
        # идёт десятками секунд. Потолок на вызов дешевле любых оптимизаций.
        out = out[:MAX_RESULT_CHARS] + "\n…ответ обрезан, уточни запрос или возьми другой источник"
    return out


_ENV_REF = re.compile(r"\$\{?(\w+)\}?")


def _resolve(config: dict) -> str | None:
    """Подставить переменные окружения. Ключи API живут в .env, не в SKILL.md."""
    url = os.path.expandvars(config["url"])
    if "$" in url:
        return None
    # Подставленное значение — ключ в URL: ошибка транспорта повторяет URL целиком,
    # и без регистрации ключ ушёл бы в результат инструмента и в лог.
    for name in _ENV_REF.findall(config["url"]):
        register_secret(os.environ.get(name, ""))
    return url


def build_tools(config: dict, safety: Safety, skill_name: str) -> list[Tool]:
    """Спросить у сервера его инструменты. Недоступный сервер не должен ронять запуск —
    скилл останется плейбуком без инструментов, о чём будет запись в логе."""
    url = _resolve(config)
    if url is None:
        log.warning("mcp_env_missing", skill=skill_name, url=config["url"])
        return []
    # Под защитой и сборка, а не только запрос: сервер чужой, и сюрприз в его
    # ответе не должен ронять запуск бота — навык просто останется без инструментов.
    # Идентичность сервера — навык и хост, без пути и query: там бывает ключ.
    server_id = f"{skill_name}:{urlsplit(url).hostname}"
    try:
        specs = _run(asyncio.wait_for(_list_tools(url), TIMEOUT))
        tools = [t for spec in specs
                 if (t := _remote_tool(url, server_id, spec, safety, skill_name)) is not None]
    except Exception as e:
        log.warning("mcp_unavailable", skill=skill_name, error=f"{type(e).__name__}: {e}")
        return []
    log.info("mcp_tools", skill=skill_name, tools=[t.name for t in tools], safety=safety.value)
    return tools
