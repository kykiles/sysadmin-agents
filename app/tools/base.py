import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from pydantic import BaseModel, ValidationError


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

    def schema(self) -> dict:
        parameters = self.params_model.model_json_schema()
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

    async def execute(self, raw_args: dict) -> str:
        try:
            params = self.params_model.model_validate(raw_args or {})
        except ValidationError as e:
            return json.dumps({"error": e.errors(include_url=False)})
        result = await self.fn(**params.model_dump())
        return _to_json(result)


def _to_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        return str(value)
