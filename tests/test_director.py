import asyncio
from app.agents.director import Director
from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result
from app.llm.client import ChoiceMessage


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


async def test_director_delegates_and_summarizes():
    reg = AgentRegistry()
    class FakeSys:
        name = "sysadmin"
        async def handle(self, task: Task) -> Result:
            return Result(task_id=task.id, content="logs are clean")
    reg.register(FakeSys())
    asyncio.create_task(reg._consume("sysadmin"))

    import json
    from app.llm.client import ToolCall, ToolCallFunction
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="delegate_to", arguments=json.dumps({"agent_name": "sysadmin", "task": "check logs"})))])
    final = ChoiceMessage(content="Отчёт: логи чисты.", tool_calls=None)
    director = Director(llm=FakeLLM([tc, final]), registry=reg, available_agents={"sysadmin": "Docker admin"})
    res = await director.handle(Task(content="посмотри логи"))
    await reg.stop()
    assert "Отчёт" in res.content


def test_director_wires_memory():
    class DummyMem:
        def load(self): return []
        def append(self, r, c): ...

    mem = DummyMem()
    reg = AgentRegistry()
    d = Director(llm=None, registry=reg, available_agents={}, memory=mem)
    assert d._memory is mem
