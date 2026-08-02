import asyncio

from app.agents.base import Agent
from app.agents.director import Director
from app.agents.messages import Result, Task


def test_director_wires_memory():
    class DummyMem:
        def load(self): return []
        def append(self, r, c): ...

    mem = DummyMem()
    d = Director(llm=None, memory=mem)
    assert d._memory is mem


async def test_tasks_run_one_at_a_time(monkeypatch):
    """Накопители Директора живут на инстансе — параллельный handle их бы перемешал.

    Раньше очередь по одной задаче обеспечивал реестр, теперь — собственный замок.
    """
    inside = 0
    overlapped = False

    async def fake_handle(self, task: Task) -> Result:
        nonlocal inside, overlapped
        inside += 1
        overlapped = overlapped or inside > 1
        await asyncio.sleep(0)  # даём второй задаче шанс влезть
        inside -= 1
        return Result(task_id=task.id, content="ok")

    monkeypatch.setattr(Agent, "handle", fake_handle)
    monkeypatch.setattr("app.agents.director._memory_index", lambda: "")

    d = Director(llm=None)
    await asyncio.gather(*(d.handle(Task(content=f"t{i}")) for i in range(5)))

    assert not overlapped
