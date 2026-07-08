import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from pydantic import BaseModel, ValidationError


class Safety(str, Enum):
    SAFE = "safe"
    DANGEROUS = "dangerous"


@dataclass
class Tool:
    name: str
    description: str
    params_model: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]
    safety: Safety = Safety.SAFE

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_model.model_json_schema(),
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
