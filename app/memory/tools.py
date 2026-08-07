from collections.abc import Callable

from pydantic import BaseModel, Field

from app.memory.facts import get_store
from app.tools.base import Tool, Safety


class RememberParams(BaseModel):
    scope: str = Field(
        description="topic this fact belongs to, taken from the memory index in your prompt; "
                    'a new one only if none fits. A topic, not a host — "global" is a last resort'
    )
    key: str = Field(description="short snake_case key, e.g. postgres_version")
    value: str = Field(description="the fact value")
    kind: str = Field(
        default="stable",
        description='"stable" for topology, paths, decisions; "snapshot" for values that drift '
                    "(versions, ports, sizes) — those are re-checked sooner",
    )


class RecallParams(BaseModel):
    scope: str | None = Field(default=None, description="filter by scope (topic)")
    query: str | None = Field(default=None, description="substring filter over key and value")


async def recall_facts(scope: str | None = None, query: str | None = None) -> dict:
    return {"facts": get_store().recall(scope=scope, query=query)}


def build_tools(tainted: Callable[[], bool] | None = None) -> list[Tool]:
    """Инструменты памяти. Не скилл: память принадлежит Директору и временным
    агентам не выдаётся — забывать факты человек решает кнопкой в Telegram.

    `tainted` — предикат «в этой задаче работал скил с недоверенным выводом».
    Факт, записанный по итогам такой задачи, помечается и всплывает на самопроверке:
    подтверждать каждую запись человеком слишком дорого, а молча верить веб-странице,
    которая осядет в памяти навсегда, нельзя.
    """

    async def remember_fact(scope: str, key: str, value: str, kind: str = "stable") -> dict:
        dirty = bool(tainted and tainted())
        get_store().remember(scope, key, value, kind, tainted=dirty)
        return {"remembered": {"scope": scope, "key": key, "value": value, "kind": kind},
                **({"note": "источник недоверенный — факт помечен для проверки"} if dirty else {})}

    return [
        Tool("recall_facts", "Recall stored facts (all, by scope, or by query substring). Safe.", RecallParams, recall_facts, Safety.SAFE),
        Tool("remember_fact", "Store a durable fact that will be needed in a future task (upserts by scope+key). Safe.", RememberParams, remember_fact, Safety.SAFE),
    ]
