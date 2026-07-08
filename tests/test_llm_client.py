import json
from unittest.mock import AsyncMock, patch
from app.llm.client import LLMClient


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
