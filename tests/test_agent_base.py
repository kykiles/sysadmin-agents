import json
from unittest.mock import AsyncMock
from pydantic import BaseModel
from app.agents.base import Agent
from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result, ConfirmationRequest
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

    async def chat(self, messages, tools=None):
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
        async def request(self, req: ConfirmationRequest) -> bool:
            return False

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="rejected", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    reg.set_confirmation_gateway(NoGateway())
    agent = Agent(name="t", system_prompt="sys", tools=[dt], llm=llm, registry=reg)
    res = await agent.handle(Task(content="restart bot"))
    assert res.success is True
    assert "rejected" in res.content
