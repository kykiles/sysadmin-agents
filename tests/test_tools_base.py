import json
from pydantic import BaseModel
from app.tools.base import Tool, Safety


class EchoParams(BaseModel):
    text: str


async def _echo(text: str) -> dict:
    return {"echo": text}


async def test_schema_format():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    s = t.schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "echo"
    assert s["function"]["parameters"]["properties"]["text"]["type"] == "string"


async def test_execute_valid():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    out = await t.execute({"text": "hi"})
    assert json.loads(out) == {"echo": "hi"}


async def test_execute_invalid_returns_error():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    out = json.loads(await t.execute({}))
    assert "error" in out


async def test_default_safety_is_safe():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    assert t.safety is Safety.SAFE
