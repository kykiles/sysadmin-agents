"""Ш6: эпизод задачи — что пробовали и что не вышло."""
import json

import pytest
from pydantic import BaseModel

from app.agents.base import Agent
from app.agents.episode import Episode
from app.agents.messages import Task, ConfirmationRequest, Decision
from app.config import settings
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.tools.base import Tool, Safety


class P(BaseModel):
    x: str = ""


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


def _call(name: str, cid: str = "c1", **args) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id=cid, function=ToolCallFunction(name=name, arguments=json.dumps(args)))])


def _flaky(outputs: list[dict]) -> Tool:
    """Инструмент, отдающий заготовленные результаты по очереди."""
    async def _fn(x: str = "") -> dict:
        return outputs.pop(0)

    return Tool("probe", "d", P, _fn, Safety.SAFE)


def test_outcome_is_ok_without_problems():
    e = Episode()
    assert e.outcome() == "ok"
    assert e.problems() == []


def test_outcome_partial_on_refusal_and_failed_on_break():
    e = Episode()
    e.refusal("host_exec: host=vpn")
    assert e.outcome() == "partial"
    e.broke("лимит итераций")
    assert e.outcome() == "failed"


def test_problems_are_capped_and_deduplicated():
    e = Episode()
    for _ in range(3):
        e.refusal("host_exec: host=vpn")
    for i in range(10):
        e.plan_left([f"пункт «{i}» — пропущен"])
    assert len(e.problems()) == 8
    assert e.problems()[0] == "отказ в подтверждении: host_exec: host=vpn"


async def test_user_refusal_becomes_partial_with_target():
    class NoGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.REJECTED

    async def _restart(container: str, _intent: str = "") -> dict:
        return {"ok": True}

    class Q(BaseModel):
        container: str

    tool = Tool("docker_restart", "d", Q, _restart, Safety.DANGEROUS)
    agent = Agent(name="t", system_prompt="sys", tools=[tool],
                  llm=FakeLLM([_call("docker_restart", container="panel", _intent="Перезапущу."),
                               ChoiceMessage(content="не вышло", tool_calls=None)]),
                  gateway=NoGateway())
    await agent.handle(Task(content="перезапусти panel"))

    assert agent._episode.outcome() == "partial"
    assert agent._episode.problems() == [
        "отказ в подтверждении: docker_restart: container=panel"
    ]


async def test_tool_error_forgotten_after_successful_retry():
    tool = _flaky([{"error": "connection refused"}, {"ok": True}])
    agent = Agent(name="t", system_prompt="sys", tools=[tool],
                  llm=FakeLLM([_call("probe", "c1"), _call("probe", "c2"),
                               ChoiceMessage(content="готово", tool_calls=None)]))
    await agent.handle(Task(content="проверь"))

    assert agent._episode.problems() == []
    assert agent._episode.outcome() == "ok"


async def test_tool_error_without_retry_stays_partial():
    tool = _flaky([{"error": "connection refused"}])
    agent = Agent(name="t", system_prompt="sys", tools=[tool],
                  llm=FakeLLM([_call("probe"), ChoiceMessage(content="не смог", tool_calls=None)]))
    await agent.handle(Task(content="проверь"))

    assert agent._episode.outcome() == "partial"
    assert agent._episode.problems() == ["ошибка probe: connection refused"]


async def test_iteration_limit_is_failed(monkeypatch):
    monkeypatch.setattr(settings, "agent_max_iterations", 2)
    tool = _flaky([{"ok": 1}, {"ok": 2}])
    agent = Agent(name="t", system_prompt="sys", tools=[tool],
                  llm=FakeLLM([_call("probe", "c1"), _call("probe", "c2")]))
    await agent.handle(Task(content="крутись"))

    assert agent._episode.outcome() == "failed"
    assert "лимит итераций" in agent._episode.problems()[0]


async def test_spawned_agent_limit_is_partial(monkeypatch):
    """Подагент на лимите — проблема задачи, а не обрыв: Директор может доделать."""
    monkeypatch.setattr(settings, "agent_max_iterations", 2)
    episode = Episode()
    tool = _flaky([{"ok": 1}, {"ok": 2}])
    agent = Agent(name="spawned:host", system_prompt="sys", tools=[tool],
                  llm=FakeLLM([_call("probe", "c1"), _call("probe", "c2")]),
                  episode=episode)
    await agent.handle(Task(content="крутись"))

    assert episode.outcome() == "partial"
    assert episode.problems() == [
        "spawned:host: достигнут лимит итераций (2), ответ может быть неполным"
    ]
