import copy
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from pydantic import BaseModel, ValidationError

from app.logging import redact, scrub


class Safety(str, Enum):
    SAFE = "safe"
    DANGEROUS = "dangerous"


# Служебный параметр, который агент обязан заполнить при вызове DANGEROUS-инструмента:
# строгое пояснение для пользователя. Вырезается перед выполнением (см. agents/base.py).
INTENT_FIELD = "_intent"
_INTENT_SCHEMA = {
    "type": "string",
    "description": (
        "Обязательно. Одно строгое предложение по-русски для пользователя — "
        "как краткий доклад руководителю: что именно ты сейчас сделаешь и зачем. "
        "Без технического жаргона и без самой команды. "
        "Пример: «Проверю список запланированных cron-задач на сервере.»"
    ),
}


@dataclass
class Tool:
    name: str
    description: str
    params_model: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]
    safety: Safety = Safety.SAFE
    # Готовая JSON-схема параметров вместо выведенной из params_model. Нужна
    # инструментам, схему которых задаём не мы, — они приходят от MCP-сервера.
    params_schema: dict | None = None

    def schema(self) -> dict:
        parameters = self.params_schema or self.params_model.model_json_schema()
        if self.safety is Safety.DANGEROUS:
            parameters = dict(parameters)
            parameters["properties"] = {
                **parameters.get("properties", {}),
                INTENT_FIELD: _INTENT_SCHEMA,
            }
            required = list(parameters.get("required", []))
            if INTENT_FIELD not in required:
                required.append(INTENT_FIELD)
            parameters["required"] = required
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def prepare(self, raw_args: dict) -> dict:
        """Проверить аргументы и вернуть снимок, который показывают и исполняют.

        `_intent` — пояснение для человека, не аргумент: вырезается до проверки
        (у MCP-модели extra разрешены, и он ушёл бы на сервер). ValidationError —
        аргументы невалидны, до подтверждения такой вызов не доходит.
        """
        args = {k: v for k, v in (raw_args or {}).items() if k != INTENT_FIELD}
        return copy.deepcopy(self.params_model.model_validate(args).model_dump())

    async def invoke(self, prepared: dict) -> str:
        """Исполнить подготовленный снимок. Без повторной валидации — она могла бы
        подставить другие defaults; копия — чтобы fn не поменял сам снимок."""
        try:
            result = await self.fn(**copy.deepcopy(prepared))
        except Exception as e:
            # ошибку отдаём агенту как результат вызова, а не роняем всю задачу:
            # он увидит причину и попробует другой путь
            return json.dumps({"error": redact(f"{type(e).__name__}: {e}")}, ensure_ascii=False)
        # Результат уходит в контекст модели, а оттуда — в историю, отчёт и память.
        # Секреты держат адаптеры; здесь — дополнительный слой на весь объект.
        return _to_json(scrub(result))

    async def execute(self, raw_args: dict) -> str:
        try:
            prepared = self.prepare(raw_args)
        except ValidationError as e:
            return json.dumps({"error": e.errors(include_url=False)})
        return await self.invoke(prepared)


def _to_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        return str(value)
