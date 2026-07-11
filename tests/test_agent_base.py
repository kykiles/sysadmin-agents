import json
from unittest.mock import AsyncMock
from pydantic import BaseModel
from app.agents.base import Agent
from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result, ConfirmationRequest, Decision
from app.tools.base import Tool, Safety
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction


class P(BaseModel):
    x: str


async def _fn(x: str) -> dict:
    return {"got": x}


def make_tool():
    return Tool(name="echo", description="d", params_model=P, fn=_fn, safety=Safety.SAFE)


class FakeLLM:
    def __init__(self, responses):
        self._r = responses
        self.last_messages = None

    async def chat(self, messages, tools=None):
        self.last_messages = list(messages)
        return self._r.pop(0)


async def test_agent_runs_safe_tool_then_answers():
    tool = make_tool()
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))])
    final = ChoiceMessage(content="result: hi", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm, registry=reg)
    res = await agent.handle(Task(content="do it"))
    assert res.success is True
    assert "result" in res.content


async def test_dangerous_rejected():
    class Q(BaseModel):
        c: str

    async def _danger(c: str) -> dict:
        return {"done": c}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class NoGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.REJECTED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="rejected", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    reg.set_confirmation_gateway(NoGateway())
    agent = Agent(name="t", system_prompt="sys", tools=[dt], llm=llm, registry=reg)
    res = await agent.handle(Task(content="restart bot"))
    assert res.success is True
    assert "rejected" in res.content


async def test_dangerous_action_is_audited(tmp_path, monkeypatch):
    from app import audit

    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))

    class Q(BaseModel):
        c: str

    async def _danger(c: str) -> dict:
        return {"returncode": 0, "done": c}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class YesGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.APPROVED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="готово", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    reg.set_confirmation_gateway(YesGateway())
    agent = Agent(name="hostadmin", system_prompt="sys", tools=[dt], llm=llm, registry=reg)
    await agent.handle(Task(content="restart bot"))
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "hostadmin"
    assert rec["tool"] == "restart"
    assert rec["decision"] == "approved"
    assert rec["result"]["returncode"] == 0


async def test_auto_approved_action_is_audited(tmp_path, monkeypatch):
    from app import audit

    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))

    class Q(BaseModel):
        c: str

    async def _danger(c: str) -> dict:
        return {"returncode": 0, "done": c}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class AutoGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.AUTO_APPROVED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="готово", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    reg.set_confirmation_gateway(AutoGateway())
    agent = Agent(name="hostadmin", system_prompt="sys", tools=[dt], llm=llm, registry=reg)
    await agent.handle(Task(content="restart bot"))
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["decision"] == "auto-approved"
    assert rec["result"]["returncode"] == 0


async def test_max_iterations_keeps_partial_answer(monkeypatch):
    from app.agents import base as base_mod

    monkeypatch.setattr(base_mod.settings, "agent_max_iterations", 2)
    tool = make_tool()

    def _tool_msg(i):
        return ChoiceMessage(
            content=f"работаю, шаг {i}",
            tool_calls=[ToolCall(id=f"c{i}", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))],
        )

    llm = FakeLLM([_tool_msg(1), _tool_msg(2)])
    reg = AgentRegistry()
    mem = FakeMemory()
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm, registry=reg, memory=mem)
    res = await agent.handle(Task(content="do it"))
    assert res.success is False
    assert "работаю, шаг 2" in res.content
    assert "лимит итераций" in res.content
    assert mem.items[-1]["content"] == res.content


class FakeMemory:
    def __init__(self):
        self.items = []

    def load(self):
        return list(self.items)

    def append(self, role, content):
        self.items.append({"role": role, "content": content})


async def test_agent_saves_final_turn_to_memory():
    mem = FakeMemory()
    final = ChoiceMessage(content="готово", tool_calls=None)
    llm = FakeLLM([final])
    reg = AgentRegistry()
    agent = Agent(name="d", system_prompt="sys", tools=[], llm=llm, registry=reg, memory=mem)
    await agent.handle(Task(content="сделай"))
    assert mem.items == [
        {"role": "user", "content": "сделай"},
        {"role": "assistant", "content": "готово"},
    ]


async def test_agent_loads_history_into_prompt():
    mem = FakeMemory()
    mem.append("user", "прошлый вопрос")
    mem.append("assistant", "прошлый ответ")
    final = ChoiceMessage(content="ок", tool_calls=None)
    llm = FakeLLM([final])
    reg = AgentRegistry()
    agent = Agent(name="d", system_prompt="sys", tools=[], llm=llm, registry=reg, memory=mem)
    await agent.handle(Task(content="новый"))
    contents = [m["content"] for m in llm.last_messages]
    assert "прошлый вопрос" in contents
    assert "прошлый ответ" in contents
    assert contents[0] == "sys"
    assert contents[-1] == "новый"


async def test_agent_without_memory_unchanged():
    final = ChoiceMessage(content="ответ", tool_calls=None)
    llm = FakeLLM([final])
    reg = AgentRegistry()
    agent = Agent(name="t", system_prompt="sys", tools=[], llm=llm, registry=reg)
    res = await agent.handle(Task(content="q"))
    assert res.content == "ответ"
