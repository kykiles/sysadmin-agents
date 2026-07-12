from pydantic import BaseModel, Field

from app.memory.facts import get_store
from app.tools.base import Tool, Safety


class RememberParams(BaseModel):
    scope: str = Field(description='"global" or a host identifier the fact belongs to')
    key: str = Field(description="short snake_case key, e.g. postgres_version")
    value: str = Field(description="the fact value")


class RecallParams(BaseModel):
    scope: str | None = Field(default=None, description="filter by scope (host or global)")
    query: str | None = Field(default=None, description="substring filter over key and value")


class ForgetParams(BaseModel):
    scope: str = Field(description="scope of the fact to remove")
    key: str = Field(description="key of the fact to remove")


class ForgetScopeParams(BaseModel):
    scope: str = Field(description="scope (host) to wipe all facts for, e.g. a decommissioned server")


async def remember_fact(scope: str, key: str, value: str) -> dict:
    get_store().remember(scope, key, value)
    return {"remembered": {"scope": scope, "key": key, "value": value}}


async def recall_facts(scope: str | None = None, query: str | None = None) -> dict:
    return {"facts": get_store().recall(scope=scope, query=query)}


async def forget_fact(scope: str, key: str) -> dict:
    get_store().forget(scope, key)
    return {"forgotten": {"scope": scope, "key": key}}


async def forget_facts(scope: str) -> dict:
    removed = get_store().forget_scope(scope)
    return {"forgotten_scope": scope, "removed": removed}


def build_tools() -> list[Tool]:
    return [
        Tool("recall_facts", "Recall stored infrastructure facts (all, by scope, or by query substring). Safe.", RecallParams, recall_facts, Safety.SAFE),
        Tool("remember_fact", "Store a durable infrastructure fact (upserts by scope+key). Safe.", RememberParams, remember_fact, Safety.SAFE),
        Tool("forget_fact", "Delete a stored fact by scope+key. Safe.", ForgetParams, forget_fact, Safety.SAFE),
        Tool("forget_facts", "Delete ALL facts for a scope (e.g. a decommissioned host). Safe.", ForgetScopeParams, forget_facts, Safety.SAFE),
    ]
