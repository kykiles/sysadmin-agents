from pydantic import BaseModel, Field

from app.memory.facts import get_store
from app.tools.base import Tool, Safety


class RememberParams(BaseModel):
    scope: str = Field(description='"global" or a host identifier the fact belongs to')
    key: str = Field(description="short snake_case key, e.g. postgres_version")
    value: str = Field(description="the fact value")
    kind: str = Field(
        default="stable",
        description='"stable" for topology, paths, decisions; "snapshot" for values that drift '
                    "(versions, ports, sizes) — those are re-checked sooner",
    )


class RecallParams(BaseModel):
    scope: str | None = Field(default=None, description="filter by scope (host or global)")
    query: str | None = Field(default=None, description="substring filter over key and value")


async def remember_fact(scope: str, key: str, value: str, kind: str = "stable") -> dict:
    get_store().remember(scope, key, value, kind)
    return {"remembered": {"scope": scope, "key": key, "value": value, "kind": kind}}


async def recall_facts(scope: str | None = None, query: str | None = None) -> dict:
    return {"facts": get_store().recall(scope=scope, query=query)}


def build_tools() -> list[Tool]:
    """Инструменты памяти. Не скилл: память принадлежит Директору и временным
    агентам не выдаётся — забывать факты человек решает кнопкой в Telegram."""
    return [
        Tool("recall_facts", "Recall stored infrastructure facts (all, by scope, or by query substring). Safe.", RecallParams, recall_facts, Safety.SAFE),
        Tool("remember_fact", "Store a durable infrastructure fact (upserts by scope+key). Safe.", RememberParams, remember_fact, Safety.SAFE),
    ]
