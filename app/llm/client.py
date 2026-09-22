import asyncio
import time
from dataclasses import dataclass, field
from openai import AsyncOpenAI, APIError, NOT_GIVEN

from app.logging import get_logger

log = get_logger("llm")


@dataclass
class ToolCallFunction:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: ToolCallFunction


@dataclass
class Usage:
    """Цена ходов модели: один ответ (`calls=1`) или сумма нескольких.

    `cost` в деньгах даёт не всякий провайдер (OpenRouter — да, в `usage.cost`);
    если не вернул, остаётся 0, а токены есть всегда.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    calls: int = 0
    # Часть prompt_tokens, пришедшая из кэша провайдера: она в разы дешевле, и без
    # неё по одним токенам не понять, сколько на деле стоит растущий контекст агента.
    cached_tokens: int = 0

    def __iadd__(self, other: "Usage") -> "Usage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost += other.cost
        self.calls += other.calls
        self.cached_tokens += other.cached_tokens
        return self


@dataclass
class ChoiceMessage:
    content: str | None
    tool_calls: list[ToolCall] | None
    # Thinking-модели требуют вернуть свои размышления обратно в истории, иначе
    # апстрим отвечает 400. Поле нестандартное — в SDK его нет, только в extra.
    reasoning_content: str | None = None
    usage: Usage = field(default_factory=Usage)


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout: float = 360, max_retries: int = 1):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url,
                                   timeout=timeout, max_retries=max_retries)
        self._model = model

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ChoiceMessage:
        # Шлюз иногда заворачивает моргание апстрима в 400 "Upstream request
        # failed" — не наша ошибка запроса, а транзиент. SDK такой 400 не
        # ретраит, поэтому дожимаем сами.
        # ponytail: 3 попытки, фикс-бэкофф; вынести в настройки если понадобится
        started = time.monotonic()
        for attempt in range(3):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=tools if tools else NOT_GIVEN,
                    # Без этого OpenRouter вернёт только токены, а цену ходов
                    # пришлось бы считать по прайсу модели вручную.
                    extra_body={"usage": {"include": True}},
                )
                break
            except APIError as e:
                if attempt == 2 or "Upstream request failed" not in str(e):
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        usage = _usage(resp)
        _log_call(self._model, resp, usage, int((time.monotonic() - started) * 1000))
        m = resp.choices[0].message
        tool_calls = None
        if m.tool_calls:
            tool_calls = [
                ToolCall(id=tc.id, function=ToolCallFunction(
                    name=tc.function.name, arguments=tc.function.arguments))
                for tc in m.tool_calls
            ]
        return ChoiceMessage(
            content=m.content,
            tool_calls=tool_calls,
            reasoning_content=getattr(m, "reasoning_content", None),
            usage=usage,
        )


def _log_call(model: str, resp, usage: Usage, ms: int) -> None:
    """Ход модели — главная статья времени задачи, а без строки лога не видно,
    долго ли он шёл и не обрезан ли по длине (обрезанный JSON аргументов)."""
    finish = getattr(resp.choices[0], "finish_reason", None)
    details = getattr(getattr(resp, "usage", None), "completion_tokens_details", None)
    fields = dict(model=model, ms=ms, prompt=usage.prompt_tokens,
                  completion=usage.completion_tokens,
                  reasoning=getattr(details, "reasoning_tokens", None),
                  cached=usage.cached_tokens, finish=finish)
    if finish == "length":
        log.warning("llm_call_truncated", **fields)
    else:
        log.info("llm_call", **fields)


def _usage(resp) -> Usage:
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage(calls=1)
    return Usage(
        prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(u, "completion_tokens", 0) or 0,
        # Нестандартное поле шлюза: в SDK его нет, в extra приходит числом.
        cost=float(getattr(u, "cost", 0.0) or 0.0),
        calls=1,
        cached_tokens=getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
    )
