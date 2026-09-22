import json
from unittest.mock import AsyncMock, patch

from structlog.testing import capture_logs

from app.llm.client import LLMClient, Usage


async def test_chat_returns_text():
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    with patch.object(client._client.chat.completions, "create", new=AsyncMock(return_value=type("R", (), {"choices": [type("C", (), {"message": fake_msg})()]})())):
        msg = await client.chat([{"role": "user", "content": "hello"}])
        assert msg.content == "hi"
        assert msg.tool_calls is None


async def test_chat_returns_tool_calls():
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    tc = type("F", (), {"name": "docker_ps", "arguments": json.dumps({})})()
    fake_msg = type("M", (), {"content": None, "tool_calls": [type("T", (), {"id": "c1", "function": tc})()]})()
    with patch.object(client._client.chat.completions, "create", new=AsyncMock(return_value=type("R", (), {"choices": [type("C", (), {"message": fake_msg})()]})())):
        msg = await client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "docker_ps"}}])
        assert msg.tool_calls[0].function.name == "docker_ps"


def _resp(message, usage=None):
    fields = {"choices": [type("C", (), {"message": message})()]}
    if usage is not None:
        fields["usage"] = usage
    return type("R", (), fields)()


async def test_chat_reports_usage():
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    usage = type("U", (), {"prompt_tokens": 120, "completion_tokens": 8, "cost": 0.0004})()
    with patch.object(client._client.chat.completions, "create",
                      new=AsyncMock(return_value=_resp(fake_msg, usage))):
        msg = await client.chat([{"role": "user", "content": "hello"}])
    assert msg.usage == Usage(prompt_tokens=120, completion_tokens=8, cost=0.0004, calls=1)


async def test_chat_reports_cached_prompt_tokens():
    """Кэш провайдера: часть входа, пришедшая из кэша, стоит в разы дешевле —
    без неё 190k токенов агента не переводятся в деньги."""
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    details = type("D", (), {"cached_tokens": 100})()
    usage = type("U", (), {"prompt_tokens": 120, "completion_tokens": 8, "cost": 0.0004,
                           "prompt_tokens_details": details})()
    with patch.object(client._client.chat.completions, "create",
                      new=AsyncMock(return_value=_resp(fake_msg, usage))):
        msg = await client.chat([{"role": "user", "content": "hello"}])
    assert msg.usage.cached_tokens == 100


async def test_chat_without_usage_still_counts_the_call():
    """Не всякий провайдер отдаёт usage — ход всё равно случился и стоил денег."""
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    with patch.object(client._client.chat.completions, "create",
                      new=AsyncMock(return_value=_resp(fake_msg))):
        msg = await client.chat([{"role": "user", "content": "hello"}])
    assert msg.usage == Usage(calls=1)


def test_timeout_and_retries_reach_sdk():
    """Дефолт SDK — 600 с × 2 повтора: зависший ход держал задачу больше 10 минут."""
    client = LLMClient(api_key="k", base_url="http://x", model="m", timeout=360, max_retries=1)
    assert client._client.timeout == 360
    assert client._client.max_retries == 1


async def test_chat_logs_duration_tokens_and_finish_reason():
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    details = type("D", (), {"reasoning_tokens": 5})()
    usage = type("U", (), {"prompt_tokens": 120, "completion_tokens": 8,
                           "completion_tokens_details": details})()
    choice = type("C", (), {"message": fake_msg, "finish_reason": "stop"})()
    resp = type("R", (), {"choices": [choice], "usage": usage})()
    with capture_logs() as logs, patch.object(client._client.chat.completions, "create",
                                              new=AsyncMock(return_value=resp)):
        await client.chat([{"role": "user", "content": "hello"}])
    [entry] = [e for e in logs if e["event"] == "llm_call"]
    assert entry["model"] == "m" and entry["finish"] == "stop"
    assert entry["prompt"] == 120 and entry["completion"] == 8 and entry["reasoning"] == 5
    assert isinstance(entry["ms"], int)


async def test_chat_warns_when_answer_cut_by_length():
    """Обрезка по длине — это обрезанный JSON аргументов инструмента."""
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": None, "tool_calls": None})()
    choice = type("C", (), {"message": fake_msg, "finish_reason": "length"})()
    with capture_logs() as logs, patch.object(client._client.chat.completions, "create",
                                              new=AsyncMock(return_value=type("R", (), {"choices": [choice]})())):
        await client.chat([{"role": "user", "content": "hello"}])
    assert [e["log_level"] for e in logs if e["event"] == "llm_call_truncated"] == ["warning"]
