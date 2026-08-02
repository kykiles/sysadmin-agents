import json
from pydantic import BaseModel
from app.tools.base import Tool, Safety, INTENT_FIELD


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


def test_safe_tool_has_no_intent_field():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    props = t.schema()["function"]["parameters"]["properties"]
    assert INTENT_FIELD not in props


def test_dangerous_tool_requires_intent():
    t = Tool(name="rm", description="d", params_model=EchoParams, fn=_echo, safety=Safety.DANGEROUS)
    params = t.schema()["function"]["parameters"]
    assert INTENT_FIELD in params["properties"]
    assert INTENT_FIELD in params["required"]


async def test_intent_is_ignored_by_execute():
    t = Tool(name="rm", description="d", params_model=EchoParams, fn=_echo, safety=Safety.DANGEROUS)
    out = await t.execute({"text": "hi", INTENT_FIELD: "Удалю файл."})
    assert json.loads(out) == {"echo": "hi"}


async def test_execute_returns_error_instead_of_raising():
    """Падение тулза не должно ронять задачу — агент получает ошибку как результат."""
    import json
    from pydantic import BaseModel
    from app.tools.base import Tool

    class NoParams(BaseModel):
        pass

    async def boom():
        raise FileNotFoundError(2, "No such file or directory", "ps")

    out = await Tool("t", "d", NoParams, boom).execute({})
    assert json.loads(out)["error"].startswith("FileNotFoundError")


def test_host_tools_run_on_host_not_in_container():
    from app.tools.docker import host_exec
    from app.skills.host.tools import ACCESS
    from app.skills.readonly import build_host_tools

    by_name = {t.name: t.fn for t in build_host_tools(ACCESS)}
    assert by_name["shell_exec"] is host_exec
