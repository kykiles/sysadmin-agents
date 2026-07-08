import json
from app.agents.sysadmin import ChiefSysadmin
from app.agents.registry import AgentRegistry
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.tools import docker as dk


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


async def test_sysadmin_has_docker_tools(monkeypatch):
    reg = AgentRegistry()
    agent = ChiefSysadmin(llm=FakeLLM([]), registry=reg)
    names = {t.name for t in agent.tools}
    assert {"docker_ps", "docker_logs", "docker_restart", "shell_exec"} <= names


async def test_sysadmin_calls_docker_ps(monkeypatch):
    async def fake_ps():
        return {"containers": [{"Names": ["bot"], "State": "running"}]}
    monkeypatch.setattr(dk, "docker_ps", fake_ps)
    reg = AgentRegistry()
    agent = ChiefSysadmin(llm=FakeLLM([
        ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="docker_ps", arguments=json.dumps({})))]),
        ChoiceMessage(content="Найден контейнер bot, работает.", tool_calls=None),
    ]), registry=reg)
    res = await agent.handle(Task(content="покажи контейнеры"))
    assert "bot" in res.content
