import asyncio
import pytest
from app.agents.messages import Task, Result, ConfirmationRequest
from app.agents.registry import AgentRegistry, ConfirmationGateway


class FakeAgent:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def handle(self, task: Task) -> Result:
        result = self._handler(task)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class FakeGateway:
    def __init__(self, decision: bool):
        self.decision = decision
        self.received: list[ConfirmationRequest] = []

    async def request(self, req: ConfirmationRequest) -> bool:
        self.received.append(req)
        return self.decision


async def test_delegate_returns_result():
    reg = AgentRegistry()
    reg.register(FakeAgent("sysadmin", lambda t: Result(task_id=t.id, content="done")))
    reg.set_confirmation_gateway(FakeGateway(True))
    asyncio.create_task(reg._consume("sysadmin"))
    res = await reg.request("sysadmin", Task(content="hi"))
    await reg.stop()
    assert res.content == "done"


async def test_confirm_routes_to_gateway():
    reg = AgentRegistry()
    gw = FakeGateway(False)
    reg.set_confirmation_gateway(gw)
    decision = await reg.confirm(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={}, description="d"))
    assert decision is False
    assert gw.received[0].tool_name == "docker_restart"
