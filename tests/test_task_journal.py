import json
import sqlite3
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.agents.director import Director
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.memory.journal import TaskJournal
from app.skills.loader import Skill
from app.tools.base import Tool, Safety


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


class _P(BaseModel):
    pass


async def _noop() -> dict:
    return {"ok": True}


def _skill_with(*tool_names: str) -> dict[str, Skill]:
    tools = [Tool(n, "t", _P, _noop, Safety.SAFE) for n in tool_names]
    return {"ops": Skill(name="ops", description="операции", instructions="## ops", tools=tools)}


def _journal(tmp_path) -> TaskJournal:
    return TaskJournal(db_path=str(tmp_path / "tasks.db"))


def _spawn_call(task: str) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1",
        function=ToolCallFunction(
            name="spawn",
            arguments=json.dumps({"role": "спец", "skills": ["ops"], "task": task}),
        ),
    )])


def _sub_calls(*tool_names: str) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[
        ToolCall(id=f"s{i}", function=ToolCallFunction(name=n, arguments="{}"))
        for i, n in enumerate(tool_names)
    ])


def test_record_and_recent_roundtrip(tmp_path):
    j = _journal(tmp_path)
    j.record(task_id="t1", chat_id="c1", intent="сколько юзеров на инбаунде",
             agents=["spawned:ops"], tool_seq=["spawn", "rw_query"],
             iterations=3, success=True)
    rows = j.recent(hours=1)
    assert len(rows) == 1
    assert rows[0]["intent"] == "сколько юзеров на инбаунде"
    assert rows[0]["tool_seq"] == ["spawn", "rw_query"]
    assert rows[0]["agents"] == ["spawned:ops"]
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
                             agents=["spawned:ops"], tool_seq=["spawn"],
                             iterations=2, success=True)
    assert len(TaskJournal(path).recent(hours=1)) == 1


async def test_agent_trace_includes_spawned_tools(tmp_path):
    """Ключевой критерий стадии 1: журнал видит цепочку спавнутого агента, не только spawn."""
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            _spawn_call("посчитай юзеров"),
            _sub_calls("rw_query", "rw_query", "rw_curl_read"),
            ChoiceMessage(content="42 юзера", tool_calls=None),
            ChoiceMessage(content="На инбаунде 42 юзера.", tool_calls=None),
        ]),
        
        journal=j,
        skills=_skill_with("rw_query", "rw_curl_read"),
    )
    await director.handle(Task(content="сколько юзеров на инбаунде ноды", chat_id="c1"))

    rows = j.recent(hours=1)
    assert len(rows) == 1
    assert rows[0]["intent"] == "сколько юзеров на инбаунде ноды"
    assert rows[0]["agents"] == ["spawned:ops"]
    assert rows[0]["tool_seq"] == ["spawn", "rw_query", "rw_query", "rw_curl_read"]
    assert rows[0]["iterations"] == 2


async def test_journal_failure_does_not_break_task(tmp_path):
    class BrokenJournal:
        def record(self, **kwargs):
            raise OSError("disk full")

    director = Director(llm=FakeLLM([ChoiceMessage(content="готово", tool_calls=None)]),
                         journal=BrokenJournal())
    res = await director.handle(Task(content="привет", chat_id="c1"))
    assert res.content == "готово"


async def test_accumulators_reset_between_tasks(tmp_path):
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            _spawn_call("df"), _sub_calls("host_query"),
            ChoiceMessage(content="диск ок", tool_calls=None),
            ChoiceMessage(content="диск ок", tool_calls=None),
            _spawn_call("free"), _sub_calls("host_query"),
            ChoiceMessage(content="память ок", tool_calls=None),
            ChoiceMessage(content="память ок", tool_calls=None),
        ]),
         journal=j, skills=_skill_with("host_query"),
    )
    await director.handle(Task(content="проверь диск", chat_id="c1"))
    await director.handle(Task(content="проверь память", chat_id="c1"))

    rows = j.recent(hours=1)
    assert len(rows) == 2
    # вторая задача не должна тащить трейс первой
    assert rows[1]["tool_seq"] == ["spawn", "host_query"]
    assert rows[1]["agents"] == ["spawned:ops"]


def test_director_without_journal_records_nothing(tmp_path):
    d = Director(llm=None)
    assert d._journal is None
    assert [t.name for t in d.tools if t.name == "recall_experience"] == []


def test_search_ranks_by_relevance_and_hides_trace(tmp_path):
    j = _journal(tmp_path)
    j.record(task_id="t1", chat_id="c1", intent="сколько юзеров на инбаунде",
             agents=["spawned:remnawave+observe"], tool_seq=["spawn", "rw_query"],
             iterations=3, success=True, summary="На инбаунде 42 юзера.")
    j.record(task_id="t2", chat_id="c1", intent="проверь свободное место на диске",
             agents=["spawned:host"], tool_seq=["spawn", "host_query"],
             iterations=2, success=True, summary="Диск занят на 61%.")

    found = j.search("посчитай пользователей инбаунда")
    assert found[0]["intent"] == "сколько юзеров на инбаунде"
    assert found[0]["summary"] == "На инбаунде 42 юзера."
    assert found[0]["skills"] == ["observe", "remnawave"]
    assert "tool_seq" not in found[0]
    # признак успеха в выдачу не идёт: на живом журнале он единица почти всегда
    assert "success" not in found[0]


def test_search_returns_nothing_on_empty_or_symbol_query(tmp_path):
    j = _journal(tmp_path)
    j.record(task_id="t1", chat_id="c1", intent="перезапусти nginx", agents=[],
             tool_seq=[], iterations=1, success=True, summary="Перезапущен.")
    # пользовательский текст не должен доезжать до FTS как синтаксис
    assert j.search("") == []
    assert j.search('" NEAR ^') == []
    assert j.search('nginx "') != []


def test_search_keeps_failures(tmp_path):
    """Неудачные задачи остаются в выдаче — итог одной фразой говорит сам за себя."""
    j = _journal(tmp_path)
    j.record(task_id="t1", chat_id="c1", intent="подними контейнер panel", agents=[],
             tool_seq=[], iterations=9, success=False, summary="Не поднялся: порт занят.")
    assert j.search("контейнер panel")[0]["summary"] == "Не поднялся: порт занят."


def test_record_replaces_index_entry(tmp_path):
    j = _journal(tmp_path)
    j.record(task_id="t1", chat_id="c1", intent="проверь tls", agents=[],
             tool_seq=[], iterations=1, success=True, summary="первый итог")
    j.record(task_id="t1", chat_id="c1", intent="проверь tls", agents=[],
             tool_seq=[], iterations=1, success=True, summary="второй итог")
    found = j.search("tls")
    assert len(found) == 1
    assert found[0]["summary"] == "второй итог"


def test_old_journal_gets_summary_column(tmp_path):
    """База, созданная прошлой версией, догоняется без потери записей."""
    path = str(tmp_path / "tasks.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, ts TEXT NOT NULL, chat_id TEXT, "
            "intent TEXT NOT NULL, agent TEXT, tool_seq TEXT, iterations INTEGER, "
            "success INTEGER, reviewed INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO tasks (id, ts, chat_id, intent, agent, tool_seq, iterations, success) "
            "VALUES ('old', ?, 'c1', 'старое', '', '[]', 1, 1)",
            (datetime.now(timezone.utc).isoformat(),),
        )
    j = TaskJournal(path)
    assert [r["id"] for r in j.recent(hours=1)] == ["old"]
    j.record(task_id="new", chat_id="c1", intent="новое", agents=[], tool_seq=[],
             iterations=1, success=True, summary="итог")
    assert j.search("новое")[0]["summary"] == "итог"


async def test_director_writes_first_line_as_summary(tmp_path):
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([ChoiceMessage(
            content="На инбаунде 42 юзера.\n\n> нода: `node-3`\n> статус: `online`",
            tool_calls=None,
        )]),
        journal=j,
    )
    await director.handle(Task(content="сколько юзеров на инбаунде", chat_id="c1"))
    assert j.search("юзеров инбаунд")[0]["summary"] == "На инбаунде 42 юзера."
