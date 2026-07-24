import asyncio
from dataclasses import dataclass
from openai import AsyncOpenAI, APIError, NOT_GIVEN


@dataclass
class ToolCallFunction:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: ToolCallFunction


@dataclass
class ChoiceMessage:
    content: str | None
    tool_calls: list[ToolCall] | None


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ChoiceMessage:
        # Шлюз иногда заворачивает моргание апстрима в 400 "Upstream request
        # failed" — не наша ошибка запроса, а транзиент. SDK такой 400 не
        # ретраит, поэтому дожимаем сами.
        # ponytail: 3 попытки, фикс-бэкофф; вынести в настройки если понадобится
        for attempt in range(3):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=tools if tools else NOT_GIVEN,
                )
                break
            except APIError as e:
                if attempt == 2 or "Upstream request failed" not in str(e):
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        m = resp.choices[0].message
        tool_calls = None
        if m.tool_calls:
            tool_calls = [
                ToolCall(id=tc.id, function=ToolCallFunction(
                    name=tc.function.name, arguments=tc.function.arguments))
                for tc in m.tool_calls
            ]
        return ChoiceMessage(content=m.content, tool_calls=tool_calls)
