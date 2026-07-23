import asyncio

from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result
from app.skills.loader import load_skill


class FakeAgent:
    def __init__(self, name: str, reply: str):
        self.name = name
        self._reply = reply

    async def handle(self, task: Task) -> Result:
        return Result(task_id=task.id, content=self._reply)


def test_skill_without_tools_py_is_a_plain_playbook(tmp_path):
    d = tmp_path / "writing"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: writing\ndescription: пишет посты\n---\n\n## Навык: письмо\nПиши коротко.",
        encoding="utf-8",
    )
    skill = load_skill(d)
    assert skill.tools == []
    assert "Пиши коротко." in skill.instructions


async def test_registry_accepts_agent_after_start():
    reg = AgentRegistry()
    await reg.run_forever()
    reg.register(FakeAgent("late", "готово"))  # появился уже на ходу, как после /reload
    try:
        res = await asyncio.wait_for(reg.request("late", Task(content="t")), timeout=1)
    finally:
        await reg.stop()
    assert res.content == "готово"


async def test_reregistered_agent_replaces_the_old_one():
    reg = AgentRegistry()
    reg.register(FakeAgent("worker", "старый"))
    await reg.run_forever()
    reg.register(FakeAgent("worker", "новый"))
    try:
        res = await asyncio.wait_for(reg.request("worker", Task(content="t")), timeout=1)
    finally:
        await reg.stop()
    assert res.content == "новый"
