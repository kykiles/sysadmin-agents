import json

from app.agents.director import Director, _memory_index
from app.agents.registry import AgentRegistry
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.memory import facts
from app.skills.loader import Skill
from app.tools.base import Tool, Safety
from pydantic import BaseModel


class FakeLLM:
    def __init__(self, responses):
        self._r = responses
        self.seen: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.seen.append(messages)
        return self._r.pop(0)


class EchoParams(BaseModel):
    text: str


async def _echo(text: str) -> dict:
    return {"echo": text}


def _skill() -> dict[str, Skill]:
    tool = Tool("echo", "echo back", EchoParams, _echo, Safety.SAFE)
    return {"writer": Skill(name="writer", description="пишет тексты",
                            instructions="## Навык: письмо", tools=[tool])}


def _call(name: str, args: dict) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1", function=ToolCallFunction(name=name, arguments=json.dumps(args)))])


async def test_spawn_runs_temporary_agent(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    # Директор спавнит агента, тот вызывает echo и отвечает.
    llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши пост"}),
        _call("echo", {"text": "пост"}),
        ChoiceMessage(content="готово", tool_calls=None),
        ChoiceMessage(content="Пост готов.", tool_calls=None),
    ])
    d = Director(llm=llm, registry=AgentRegistry(), available_agents={}, skills=_skill())
    res = await d.handle(Task(content="сделай пост"))

    assert res.content == "Пост готов."
    assert d._agents_used == ["spawned:writer"]
    assert "echo" in d._sub_trace
    # У временного агента свой промпт из SKILL.md и никакой истории диалога.
    assert "## Навык: письмо" in llm.seen[1][0]["content"]


async def test_spawn_rejects_unknown_skill(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["нетакого"], "task": "t"}),
        ChoiceMessage(content="навыка нет", tool_calls=None),
    ])
    d = Director(llm=llm, registry=AgentRegistry(), available_agents={}, skills=_skill())
    await d.handle(Task(content="сделай"))

    tool_reply = json.loads(llm.seen[1][-1]["content"])
    assert "нетакого" in tool_reply["error"]
    assert d._agents_used == []


def test_memory_index_lists_scopes_not_facts(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    store = facts.get_store()
    store.remember("docker", "compose_path", "/opt/app")
    store.remember("docker", "engine_version", "27.1")
    store.remember("global", "timezone", "UTC")

    idx = _memory_index()
    assert "docker: 2 фактов" in idx
    assert "global: 1 фактов" in idx
    assert "/opt/app" not in idx  # значения в промпт не попадают
