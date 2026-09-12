import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from app.agents.director import Director
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.skills import mcp_bridge
from app.skills.loader import Skill, load_skill
from app.skills.readonly import HostAccess
from app.tools.base import Safety, Tool


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


class _P(BaseModel):
    pass


async def _noop() -> dict:
    return {"ok": True}


@dataclass
class _Spec:
    name: str
    description: str
    input_schema: dict


def _write_skill(tmp_path, body: str):
    d = tmp_path / "search"
    d.mkdir()
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


FRONTMATTER = """---
name: search
description: поиск в интернете
mcp:
  url: https://example.test/mcp/?key=${TEST_MCP_KEY}
safety: safe
untrusted: true
---

## Навык: поиск
"""


def test_skill_declares_mcp_server_and_gets_its_tools(tmp_path, monkeypatch):
    seen = {}

    def fake_build(config, safety, name):
        seen.update(config=config, safety=safety, name=name)
        return [Tool("web_search", "search", _P, _noop, safety)]

    monkeypatch.setattr(mcp_bridge, "build_tools", fake_build)
    skill = load_skill(_write_skill(tmp_path, FRONTMATTER))

    assert [t.name for t in skill.tools] == ["web_search"]
    assert seen["safety"] is Safety.SAFE
    assert skill.untrusted is True


def test_missing_safety_field_means_dangerous(tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(
        mcp_bridge, "build_tools",
        lambda config, safety, name: got.setdefault("safety", safety) and [],
    )
    load_skill(_write_skill(tmp_path, FRONTMATTER.replace("safety: safe\n", "")))
    assert got["safety"] is Safety.DANGEROUS


def _serves(*specs):
    async def _list_tools(url):
        return list(specs)

    return _list_tools


def test_unreachable_server_leaves_playbook_without_tools(tmp_path, monkeypatch):
    """Недоступный сервер не должен ронять запуск бота."""
    monkeypatch.setenv("TEST_MCP_KEY", "k")

    async def _boom(url):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(mcp_bridge, "_list_tools", _boom)
    skill = load_skill(_write_skill(tmp_path, FRONTMATTER))
    assert skill.tools == []
    assert "Навык: поиск" in skill.instructions


def test_missing_env_key_is_not_sent_to_server(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_MCP_KEY", raising=False)
    called = []

    async def _spy(url):
        called.append(url)
        return []

    monkeypatch.setattr(mcp_bridge, "_list_tools", _spy)
    assert load_skill(_write_skill(tmp_path, FRONTMATTER)).tools == []
    assert called == []


async def test_url_key_does_not_leak_through_transport_error(monkeypatch):
    """Ошибка транспорта повторяет URL целиком — вместе с ключом из env."""
    monkeypatch.setenv("TEST_MCP_KEY", "mcp-url-key-AUDIT-0042")
    spec = _Spec("web_search", "искать", {"type": "object", "properties": {}})
    monkeypatch.setattr(mcp_bridge, "_list_tools", _serves(spec))

    async def _boom(url, name, args):
        raise ConnectionError(f"cannot reach {url}")

    monkeypatch.setattr(mcp_bridge, "_call_tool", _boom)
    (tool,) = mcp_bridge.build_tools(
        {"url": "https://example.test/mcp/?key=${TEST_MCP_KEY}"}, Safety.SAFE, "search")
    out = await tool.execute({})
    assert "error" in out
    assert "mcp-url-key-AUDIT-0042" not in out


async def test_huge_answer_is_capped(monkeypatch):
    """Страница целиком в контексте агента стоит десятков секунд на каждой генерации."""
    class _Chunk:
        text = "ы" * 50_000

    class _Session:
        async def call_tool(self, name, args):
            return type("R", (), {"content": [_Chunk()]})()

    @asynccontextmanager
    async def _fake(url):
        yield _Session()

    monkeypatch.setattr(mcp_bridge, "_session", _fake)
    out = await mcp_bridge._call_tool("https://example.test/mcp/", "tavily_extract", {})
    assert len(out) < mcp_bridge.MAX_RESULT_CHARS + 200
    assert out.endswith("другой источник")


def test_unexpected_spec_shape_does_not_crash_startup(monkeypatch):
    """Сервер чужой: сюрприз в его ответе должен стоить навыку инструментов, не запуска."""
    monkeypatch.setattr(mcp_bridge, "_list_tools", _serves(object()))
    assert mcp_bridge.build_tools({"url": "https://example.test/mcp/"},
                                  Safety.SAFE, "search") == []


def test_server_schema_reaches_the_model(monkeypatch):
    """Схему параметров задаёт сервер — не наша модель."""
    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}
    monkeypatch.setattr(
        mcp_bridge, "_list_tools", _serves(_Spec("web_search", "искать", schema)),
    )
    tools = mcp_bridge.build_tools({"url": "https://example.test/mcp/"}, Safety.SAFE, "search")
    assert tools[0].schema()["function"]["parameters"] == schema


def test_dangerous_mcp_tool_still_gets_intent_field(monkeypatch):
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    monkeypatch.setattr(
        mcp_bridge, "_list_tools", _serves(_Spec("write_file", "писать", schema)),
    )
    tools = mcp_bridge.build_tools({"url": "https://example.test/mcp/"}, Safety.DANGEROUS, "files")
    params = tools[0].schema()["function"]["parameters"]
    assert "_intent" in params["properties"]
    assert "_intent" in params["required"]


# ---------- идентичность вызова и проверка схемы (аудит 2026-09-12, F07) ----------

URL = "https://mcp.example/mcp/?key=k-0042-secret"
CLOSED = {"type": "object",
          "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
          "required": ["query"], "additionalProperties": False}
EMPTY_CLOSED = {"type": "object", "properties": {}, "additionalProperties": False}


def _built(monkeypatch, *specs, safety=Safety.SAFE):
    calls = []

    async def _call(url, name, args):
        calls.append((name, dict(args)))
        return "ok"

    monkeypatch.setattr(mcp_bridge, "_list_tools", _serves(*specs))
    monkeypatch.setattr(mcp_bridge, "_call_tool", _call)
    return mcp_bridge.build_tools({"url": URL}, safety, "search"), calls


async def test_name_argument_does_not_redirect_dispatch(monkeypatch):
    (tool,), calls = _built(monkeypatch, _Spec("read_status", "read", EMPTY_CLOSED))
    out = await tool.execute({"_name": "different_remote_operation"})
    assert "error" in out
    assert calls == []


async def test_declared_name_field_is_an_ordinary_argument(monkeypatch):
    schema = {"type": "object", "properties": {"_name": {"type": "string"}},
              "additionalProperties": False}
    (tool,), calls = _built(monkeypatch, _Spec("read_status", "read", schema))
    await tool.execute({"_name": "different_remote_operation"})
    assert calls == [("read_status", {"_name": "different_remote_operation"})]


@pytest.mark.parametrize("args", [
    {"query": 1},                     # неверный тип
    {},                               # нет обязательного
    {"query": "x", "extra": 1},       # лишнее поле при закрытой схеме
    {"query": "x", "limit": "5"},     # строка вместо integer
])
async def test_invalid_args_make_no_remote_call(monkeypatch, args):
    (tool,), calls = _built(monkeypatch, _Spec("web_search", "s", CLOSED))
    out = await tool.execute(args)
    assert "error" in out
    assert calls == []


async def test_invalid_args_of_dangerous_tool_are_not_even_confirmed(monkeypatch, tmp_path):
    from app import audit
    from app.agents.base import Agent
    from app.agents.messages import Decision

    monkeypatch.setattr(audit.settings, "audit_trail_path", str(tmp_path / "audit.jsonl"))
    (tool,), calls = _built(monkeypatch, _Spec("delete_page", "d", CLOSED), safety=Safety.DANGEROUS)
    asked = []

    class Yes:
        async def request(self, req):
            asked.append(req)
            return Decision.APPROVED

    call = ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1", function=ToolCallFunction(name="delete_page", arguments=json.dumps({"query": 1})))])
    llm = FakeLLM([call, ChoiceMessage(content="ок", tool_calls=None)])
    await Agent("a", "sys", [tool], llm, gateway=Yes()).handle(Task(content="x"))
    assert asked == [] and calls == []


async def test_each_tool_dispatches_its_own_remote_name(monkeypatch):
    tools, calls = _built(monkeypatch, _Spec("alpha", "a", EMPTY_CLOSED), _Spec("beta", "b", EMPTY_CLOSED))
    for tool in tools:
        await tool.execute({})
    assert [name for name, _ in calls] == ["alpha", "beta"]


def test_server_identity_is_separate_and_has_no_key(monkeypatch):
    (tool,), _ = _built(monkeypatch, _Spec("web_search", "s", CLOSED))
    assert tool.remote == ("search:mcp.example", "web_search")
    assert "k-0042-secret" not in repr(tool.remote)


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"q": {"type": "string"}}},              # нет additionalProperties
    {"type": "object", "properties": {"q": {}}, "additionalProperties": False},  # поле без типа
    {"type": "object", "additionalProperties": False,
     "properties": {"opts": {"type": "object", "properties": {}}}},           # вложенный открытый объект
    {"type": "object", "additionalProperties": False,
     "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "object"}]}}},  # не разобрать
    {"type": "object", "additionalProperties": False,
     "properties": {"tags": {"type": "array"}}},                               # массив без items
])
def test_open_or_unsupported_schema_is_not_safe(monkeypatch, schema):
    (tool,), _ = _built(monkeypatch, _Spec("web_search", "s", schema))
    assert tool.safety is Safety.DANGEROUS


def test_closed_schema_stays_safe(monkeypatch):
    nested = {"type": "object", "additionalProperties": False, "required": ["q"], "properties": {
        "q": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "opts": {"type": "object", "additionalProperties": False,
                 "properties": {"deep": {"type": "boolean"}}},
    }}
    (tool,), _ = _built(monkeypatch, _Spec("web_search", "s", nested))
    assert tool.safety is Safety.SAFE


def test_invalid_schema_skips_only_that_tool(monkeypatch):
    tools, _ = _built(monkeypatch, _Spec("broken", "b", {"type": "objekt"}),
                      _Spec("fine", "f", EMPTY_CLOSED))
    assert [t.name for t in tools] == ["fine"]


# ---------- изоляция недоверенного вывода ----------


def _library() -> dict[str, Skill]:
    return {
        "search": Skill(name="search", description="поиск", instructions="ищи",
                        tools=[Tool("web_search", "s", _P, _noop, Safety.SAFE)],
                        untrusted=True),
        "host": Skill(name="host", description="сервер", instructions="смотри",
                      tools=[], access=HostAccess(binaries=frozenset({"df"}))),
        "docker": Skill(name="docker", description="докер", instructions="крути",
                        tools=[Tool("docker_restart", "d", _P, _noop, Safety.DANGEROUS)]),
        "notes": Skill(name="notes", description="заметки", instructions="пиши",
                       tools=[Tool("note_write", "n", _P, _noop, Safety.SAFE)]),
    }


def _spawn_call(skills: list[str]) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1",
        function=ToolCallFunction(
            name="spawn",
            arguments=json.dumps({"role": "спец", "skills": skills, "task": "найди"}),
        ),
    )])


async def _spawn_result(skills: list[str]) -> dict:
    director = Director(
        llm=FakeLLM([_spawn_call(skills),
                     ChoiceMessage(content="под", tool_calls=None),
                     ChoiceMessage(content="готово", tool_calls=None)]),
        skills=_library(),
    )
    spawn = next(t for t in director.tools if t.name == "spawn")
    return json.loads(await spawn.execute(
        {"role": "спец", "skills": skills, "task": "найди"}
    ))


@pytest.mark.parametrize("skills", [["search", "host"], ["search", "docker"]])
async def test_untrusted_skill_refused_next_to_powers(skills):
    out = await _spawn_result(skills)
    assert "error" in out
    assert "search" in out["error"]
    assert "spawn" in out["how"]


async def test_untrusted_tools_run_out_of_budget():
    """Плейбук просит не частить, слабая модель просьбу игнорирует — предел механический."""
    from app.agents.director import _UNTRUSTED_CALL_BUDGET, _budgeted

    calls = []

    async def _count(**kwargs):
        calls.append(1)
        return {"ok": True}

    tools = _budgeted([Tool("web_search", "s", _P, _count, Safety.SAFE)],
                      _UNTRUSTED_CALL_BUDGET)
    outs = [await tools[0].execute({}) for _ in range(_UNTRUSTED_CALL_BUDGET + 2)]

    assert len(calls) == _UNTRUSTED_CALL_BUDGET
    assert "бюджет вызовов исчерпан" in outs[-1]
    assert "бюджет" not in outs[0]


async def test_budget_is_per_spawn_not_global():
    from app.agents.director import _budgeted

    async def _ok(**kwargs):
        return {"ok": True}

    first = _budgeted([Tool("web_search", "s", _P, _ok, Safety.SAFE)], 1)
    second = _budgeted([Tool("web_search", "s", _P, _ok, Safety.SAFE)], 1)
    await first[0].execute({})
    assert "бюджет" not in await second[0].execute({})


async def test_untrusted_skill_allowed_with_harmless_neighbour():
    out = await _spawn_result(["search", "notes"])
    assert "error" not in out
    assert out["success"] is True
