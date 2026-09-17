from collections.abc import Callable

from pydantic import BaseModel, Field

from agent_memory.facts import KnowledgeStore
from app.logging import redact
from app.tools.base import Tool, Safety


class RememberParams(BaseModel):
    scope: str = Field(
        description="topic this fact belongs to, taken from the memory index in your prompt; "
                    'a new one only if none fits. A topic, not a host — "global" is a last resort'
    )
    key: str = Field(description="short snake_case key, e.g. postgres_version")
    value: str = Field(description="the fact value")
    description: str = Field(
        default="",
        description="when this fact will be needed, one short phrase — it is what makes "
                    "the fact findable from a task worded differently than the key",
    )
    kind: str = Field(
        default="stable",
        description='"stable" for topology, paths, decisions; "snapshot" for values that drift '
                    "(versions, ports, sizes) — those are re-checked sooner",
    )


class RecallParams(BaseModel):
    scope: str | None = Field(default=None, description="filter by scope (topic)")
    query: str | None = Field(default=None, description="substring filter over key, value and description")


def build_tools(store: KnowledgeStore,
                provenance: Callable[[], dict | None] | None = None) -> list[Tool]:
    """Инструменты памяти. Не скилл: память принадлежит Директору и временным
    агентам не выдаётся — забывать факты человек решает кнопкой в Telegram.

    `provenance` — происхождение текущей задачи: None, если недоверенного вывода
    в ней не было, иначе {"run_id", "source"}. Назначает его код Директора, а не
    модель. Факт из такой задачи уходит в карантин и становится знанием только
    после одобрения владельцем: подтверждать каждую запись слишком дорого, а молча
    верить веб-странице, которая осядет в памяти навсегда, нельзя.
    """

    async def recall_facts(scope: str | None = None, query: str | None = None) -> dict:
        return {"facts": store.recall(scope=scope, query=query)}

    async def remember_fact(scope: str, key: str, value: str, description: str = "",
                            kind: str = "stable") -> dict:
        origin = provenance() if provenance else None
        # память переживает задачу и уходит в каждый следующий промпт — без секретов
        value, description = redact(value), redact(description)
        # Похожие ищем ДО записи, иначе новый факт найдёт сам себя.
        similar = store.similar(scope, key, f"{value} {description}")
        # Напоминание возвращаем в результате, а не строкой в системном промпте:
        # оно попадает в контекст ровно в тот момент, когда модель собирается
        # отчитаться пользователю о служебной записи вместо ответа на вопрос.
        note = "служебная запись; пользователю о ней не сообщай — ответь на его задачу"
        fact = {"scope": scope, "key": key, "value": value, "kind": kind}
        if origin is None:
            store.remember(scope, key, value, kind, description=description)
            out = {"remembered": fact, "note": note}
        else:
            store.propose(scope, key, value, run_id=origin["run_id"], tool="remember_fact",
                          source=origin["source"], kind=kind, description=description)
            out = {"proposed": fact, "note": note + (
                ". Источник недоверенный — факт ушёл владельцу на проверку и в памяти "
                "появится только после его одобрения")}
        if similar:
            # Запись не блокируем: двухходовка заставила бы Директора избегать
            # remember_fact. Показываем похожее — переписать под тем же ключом
            # дешевле, чем потом разбираться, какой из двух фактов верен.
            out["similar"] = similar
            out["note"] += (". В памяти есть похожее (см. similar) — если это про то же "
                            "самое, перезапиши под их ключом и забудь лишнее")
        return out

    return [
        Tool("recall_facts", "Recall stored facts (all, by scope, or by query substring). Safe.", RecallParams, recall_facts, Safety.SAFE),
        Tool("remember_fact", "Store a durable fact that will be needed in a future task (upserts by scope+key). Safe.", RememberParams, remember_fact, Safety.SAFE),
    ]
