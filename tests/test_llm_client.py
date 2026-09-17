import json
from unittest.mock import AsyncMock, patch
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


async def test_chat_without_usage_still_counts_the_call():
    """Не всякий провайдер отдаёт usage — ход всё равно случился и стоил денег."""
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    with patch.object(client._client.chat.completions, "create",
                      new=AsyncMock(return_value=_resp(fake_msg))):
        msg = await client.chat([{"role": "user", "content": "hello"}])
    assert msg.usage == Usage(calls=1)
