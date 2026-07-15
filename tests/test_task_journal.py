import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.agents.director import Director
from app.agents.messages import Task, Result
from app.agents.registry import AgentRegistry
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.memory.journal import TaskJournal


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


def _journal(tmp_path) -> TaskJournal:
    return TaskJournal(db_path=str(tmp_path / "tasks.db"))


def _delegate_call(agent_name: str, task: str) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1",
        function=ToolCallFunction(
            name="delegate_to",
            arguments=json.dumps({"agent_name": agent_name, "task": task}),
        ),
    )])


def test_record_and_recent_roundtrip(tmp_path):
    j = _journal(tmp_path)
    j.record(task_id="t1", chat_id="c1", intent="сколько юзеров на инбаунде",
             agents=["remnawave"], tool_seq=["delegate_to", "rw_query"],
             iterations=3, success=True)
    rows = j.recent(hours=1)
    assert len(rows) == 1
    assert rows[0]["intent"] == "сколько юзеров на инбаунде"
    assert rows[0]["tool_seq"] == ["delegate_to", "rw_query"]
    assert rows[0]["agents"] == ["remnawave"]
    assert rows[0]["success"] is True


def test_recent_excludes_older_than_window(tmp_path):
    path = str(tmp_path / "tasks.db")
    j = TaskJournal(path)
    j.record(task_id="fresh", chat_id="c1", intent="свежее", agents=[],
             tool_seq=[], iterations=1, success=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, ts, chat_id, intent, agent, tool_seq, iterations, success) "
            "VALUES ('old', ?, 'c1', 'старое', '', '[]', 1, 1)", (old_ts,),
        )
    assert [r["id"] for r in j.recent(hours=1)] == ["fresh"]
    assert {r["id"] for r in j.recent(hours=24)} == {"fresh", "old"}


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "tasks.db")
    TaskJournal(path).record(task_id="t1", chat_id="c1", intent="запомни",
                             agents=["hostadmin"], tool_seq=["delegate_to"],
                             iterations=2, success=True)
    assert len(TaskJournal(path).recent(hours=1)) == 1


async def test_agent_trace_includes_specialist_tools(tmp_path):
    """Ключевой критерий стадии 1: журнал видит цепочку специалиста, не только delegate_to."""
    reg = AgentRegistry()

    class FakeSpecialist:
        name = "remnawave"

        async def handle(self, task: Task) -> Result:
            return Result(task_id=task.id, content="42 юзера",
                          trace=["rw_query", "rw_query", "rw_curl_read"], iterations=4)

    reg.register(FakeSpecialist())
    asyncio.create_task(reg._consume("remnawave"))

    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([_delegate_call("remnawave", "посчитай юзеров"),
                     ChoiceMessage(content="На инбаунде 42 юзера.", tool_calls=None)]),
        registry=reg,
        available_agents={"remnawave": "VPN panel"},
        journal=j,
    )
    await director.handle(Task(content="сколько юзеров на инбаунде ноды", chat_id="c1"))
    await reg.stop()

    rows = j.recent(hours=1)
    assert len(rows) == 1
    assert rows[0]["intent"] == "сколько юзеров на инбаунде ноды"
    assert rows[0]["agents"] == ["remnawave"]
    assert rows[0]["tool_seq"] == ["delegate_to", "rw_query", "rw_query", "rw_curl_read"]
    assert rows[0]["iterations"] == 2


async def test_journal_failure_does_not_break_task(tmp_path):
    class BrokenJournal:
        def record(self, **kwargs):
            raise OSError("disk full")

    reg = AgentRegistry()
    director = Director(llm=FakeLLM([ChoiceMessage(content="готово", tool_calls=None)]),
                        registry=reg, available_agents={}, journal=BrokenJournal())
    res = await director.handle(Task(content="привет", chat_id="c1"))
    assert res.content == "готово"


async def test_accumulators_reset_between_tasks(tmp_path):
    reg = AgentRegistry()

    class FakeSpecialist:
        name = "hostadmin"

        async def handle(self, task: Task) -> Result:
            return Result(task_id=task.id, content="ok", trace=["host_query"], iterations=2)

    reg.register(FakeSpecialist())
    asyncio.create_task(reg._consume("hostadmin"))

    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            _delegate_call("hostadmin", "df"), ChoiceMessage(content="диск ок", tool_calls=None),
            _delegate_call("hostadmin", "free"), ChoiceMessage(content="память ок", tool_calls=None),
        ]),
        registry=reg, available_agents={"hostadmin": "host"}, journal=j,
    )
    await director.handle(Task(content="проверь диск", chat_id="c1"))
    await director.handle(Task(content="проверь память", chat_id="c1"))
    await reg.stop()

    rows = j.recent(hours=1)
    assert len(rows) == 2
    # вторая задача не должна тащить трейс первой
    assert rows[1]["tool_seq"] == ["delegate_to", "host_query"]
    assert rows[1]["agents"] == ["hostadmin"]


def test_director_without_journal_records_nothing(tmp_path):
    reg = AgentRegistry()
    d = Director(llm=None, registry=reg, available_agents={})
    assert d._journal is None
