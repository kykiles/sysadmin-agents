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
import threading
from contextlib import asynccontextmanager

from pydantic import BaseModel, ConfigDict

from app.logging import get_logger
from app.tools.base import Safety, Tool

log = get_logger("mcp")

TIMEOUT = 30


class AnyParams(BaseModel):
    """Аргументы описывает сервер своей JSON-схемой — нашей модели их проверять нечем."""

    model_config = ConfigDict(extra="allow")


def _run(coro):
    """Выполнить корутину синхронно, независимо от того, крутится ли уже loop.

    Навыки читаются синхронно и на старте (внутри async main), и по /reload
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
    return "\n".join(texts) if texts else "(пустой ответ)"


def _resolve(config: dict) -> str | None:
    """Подставить переменные окружения. Ключи API живут в .env, не в SKILL.md."""
    url = os.path.expandvars(config["url"])
    return None if "$" in url else url


def build_tools(config: dict, safety: Safety, skill_name: str) -> list[Tool]:
    """Спросить у сервера его инструменты. Недоступный сервер не должен ронять запуск —
    скилл останется плейбуком без инструментов, о чём будет запись в логе."""
    url = _resolve(config)
    if url is None:
        log.warning("mcp_env_missing", skill=skill_name, url=config["url"])
        return []
    try:
        specs = _run(asyncio.wait_for(_list_tools(url), TIMEOUT))
    except Exception as e:
        log.warning("mcp_unavailable", skill=skill_name, error=f"{type(e).__name__}: {e}")
        return []

    tools = []
    for spec in specs:
        async def fn(_name: str = spec.name, **kwargs) -> str:
            return await asyncio.wait_for(_call_tool(url, _name, kwargs), TIMEOUT)

        tools.append(Tool(
            name=spec.name,
            description=spec.description or spec.name,
            params_model=AnyParams,
            fn=fn,
            safety=safety,
            params_schema=spec.inputSchema,
        ))
    log.info("mcp_tools", skill=skill_name, tools=[t.name for t in tools], safety=safety.value)
    return tools
