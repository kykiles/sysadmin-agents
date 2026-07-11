import asyncio
import pytest
from app.agents.messages import Task, Result, ConfirmationRequest, Decision
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
    def __init__(self, decision: Decision):
        self.decision = decision
        self.received: list[ConfirmationRequest] = []

    async def request(self, req: ConfirmationRequest) -> Decision:
        self.received.append(req)
        return self.decision


async def test_delegate_returns_result():
    reg = AgentRegistry()
    reg.register(FakeAgent("sysadmin", lambda t: Result(task_id=t.id, content="done")))
    reg.set_confirmation_gateway(FakeGateway(Decision.APPROVED))
    asyncio.create_task(reg._consume("sysadmin"))
    res = await reg.request("sysadmin", Task(content="hi"))
    await reg.stop()
    assert res.content == "done"


async def test_consume_releases_scope_after_handle():
    released = []

    class RecordingGateway:
        async def request(self, req):
            return Decision.APPROVED
        def release(self, task_id):
            released.append(task_id)

    reg = AgentRegistry()
    reg.set_confirmation_gateway(RecordingGateway())

    def boom(t):
        raise RuntimeError("fail")

    reg.register(FakeAgent("worker", boom))
    asyncio.create_task(reg._consume("worker"))
    task = Task(content="hi")
    await reg.request("worker", task)
    await reg.stop()
    assert task.id in released


async def test_confirm_routes_to_gateway():
    reg = AgentRegistry()
    gw = FakeGateway(Decision.REJECTED)
    reg.set_confirmation_gateway(gw)
    decision = await reg.confirm(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={}, description="d"))
    assert decision is Decision.REJECTED
    assert gw.received[0].tool_name == "docker_restart"
