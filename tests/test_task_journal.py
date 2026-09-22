import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from app.agents.director import Director
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction, Usage
from agent_memory.journal import TaskJournal
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


def test_transcript_saved_and_rotated(tmp_path):
    j = TaskJournal(str(tmp_path / "t.db"))
    for n in range(3):
        j.save_transcript(f"task-{n}", f"body-{n}", keep=2)
    assert j.transcript() == ("task-2", "body-2")
    assert j.transcript(2) == ("task-1", "body-1")
    # keep=2 вытеснил самый старый
    assert j.transcript(3) is None


async def test_summary_skips_preambles_of_tool_turns(tmp_path):
    """Живой случай: Директор по дороге писал «сейчас посмотрю», и в журнал уезжала
    первая такая реплика вместо вывода задачи."""
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            ChoiceMessage(
                content="Разберусь. Сначала подниму память по базе бота.",
                tool_calls=[ToolCall(id="c1", function=ToolCallFunction(
                    name="recall_facts", arguments="{}"))],
            ),
            ChoiceMessage(
                content="Разобрал полностью: подписка активна до 2027-03-07.\n\n> детали",
                tool_calls=None,
            ),
        ]),
        journal=j,
        skills=_skill_with("recall_facts"),
    )
    result = await director.handle(Task(content="почему подписка expired", chat_id="c1"))
    # пользователю уходит всё, включая преамбулу
    assert "Разберусь." in result.content
    # а в журнал — итог финального хода
    assert j.search("подписка expired")[0]["summary"] == (
        "Разобрал полностью: подписка активна до 2027-03-07."
    )


def _two_spawns() -> ChoiceMessage:
    return ChoiceMessage(content=None, usage=Usage(100, 20, 0.01, 1, 60), tool_calls=[
        ToolCall(id=f"c{i}", function=ToolCallFunction(
            name="spawn",
            arguments=json.dumps({"role": "спец", "skills": ["ops"], "task": t}),
        ))
        for i, t in enumerate(["диск", "память"])
    ])


async def test_journal_separates_director_and_agent_cost(tmp_path):
    """Ш4: токены Директора и спавнутых агентов идут в журнал раздельно."""
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            _two_spawns(),
            ChoiceMessage(content="диск ок", tool_calls=None, usage=Usage(40, 6, 0.002, 1, 30)),
            ChoiceMessage(content="память ок", tool_calls=None, usage=Usage(40, 6, 0.002, 1)),
            ChoiceMessage(content="Всё в порядке.", tool_calls=None, usage=Usage(150, 30, 0.02, 1, 90)),
        ]),
        journal=j, skills=_skill_with("host_query"),
    )
    await director.handle(Task(content="проверь сервер", chat_id="c1"))

    with sqlite3.connect(str(tmp_path / "tasks.db")) as conn:
        row = conn.execute(
            "SELECT director_in, director_out, agents_in, agents_out, cost, llm_calls, "
            "tool_calls, spawns, duration_ms, director_cached, agents_cached FROM tasks"
        ).fetchone()
    d_in, d_out, a_in, a_out, cost, llm_calls, tool_calls, spawns, duration, d_c, a_c = row
    assert (d_in, d_out) == (250, 50)
    assert (a_in, a_out) == (80, 12)
    assert (d_c, a_c) == (150, 30)
    assert cost == pytest.approx(0.034)
    assert llm_calls == 4
    assert (tool_calls, spawns) == (2, 2)
    assert duration >= 0


# ---------- Ш6: эпизод задачи ----------

def _progress_bot():
    from unittest.mock import AsyncMock, MagicMock
    from app.bot.progress import TelegramProgress

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    bot.edit_message_text = AsyncMock()
    return TelegramProgress(bot, 1), bot


def _episode_of(tmp_path) -> tuple[str, list]:
    with sqlite3.connect(str(tmp_path / "tasks.db")) as conn:
        outcome, problems = conn.execute(
            "SELECT outcome, problems FROM tasks").fetchone()
    return outcome, json.loads(problems)


def _skill_failing(tool_name: str, error: str) -> dict[str, Skill]:
    async def _fail() -> dict:
        return {"error": error}

    return {"ops": Skill(name="ops", description="операции", instructions="## ops",
                         tools=[Tool(tool_name, "t", _P, _fail, Safety.SAFE)])}


async def test_clean_task_is_recorded_as_ok(tmp_path):
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            _spawn_call("посчитай"), _sub_calls("rw_query"),
            ChoiceMessage(content="42", tool_calls=None),
            ChoiceMessage(content="42 юзера.", tool_calls=None),
        ]),
        journal=j, skills=_skill_with("rw_query"),
    )
    await director.handle(Task(content="сколько юзеров", chat_id="c1"))
    assert _episode_of(tmp_path) == ("ok", [])


async def test_agent_tool_error_makes_task_partial(tmp_path):
    """Проблемы спавнутого агента — проблемы задачи: эпизод у них общий."""
    j = _journal(tmp_path)
    director = Director(
        llm=FakeLLM([
            _spawn_call("посмотри"), _sub_calls("rw_query"),
            ChoiceMessage(content="база не отвечает", tool_calls=None),
            ChoiceMessage(content="Не получилось: база не отвечает.", tool_calls=None),
        ]),
        journal=j, skills=_skill_failing("rw_query", "connection refused"),
    )
    await director.handle(Task(content="сколько юзеров", chat_id="c1"))
    assert _episode_of(tmp_path) == ("partial", ["ошибка rw_query: connection refused"])


async def test_unfinished_plan_step_lands_in_problems(tmp_path):
    j = _journal(tmp_path)
    progress, _bot = _progress_bot()
    director_llm = FakeLLM([
        ChoiceMessage(content=None, tool_calls=[ToolCall(id="p1", function=ToolCallFunction(
            name="plan",
            arguments=json.dumps({"title": "Проверка", "steps": ["Снять метрики", "Починить"]})))]),
        ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(
            name="spawn",
            arguments=json.dumps({"role": "спец", "skills": ["ops"], "task": "сними",
                                  "steps": [1, 2]})))]),
        ChoiceMessage(content="Метрики сняты, чинить не стал.", tool_calls=None),
    ])
    agent_llm = FakeLLM([
        ChoiceMessage(content=None, tool_calls=[ToolCall(id="m1", function=ToolCallFunction(
            name="mark_step", arguments=json.dumps({"step": 1, "status": "done"})))]),
        ChoiceMessage(content="снял", tool_calls=None),
    ])
    director = Director(llm=director_llm, agent_llm=agent_llm, journal=j,
                        skills=_skill_with("rw_query"), progress=progress)
    await director.handle(Task(content="проверь сервер", chat_id="c1"))

    assert _episode_of(tmp_path) == ("partial", ["пункт «Починить» — пропущен"])


async def test_crashed_task_is_recorded_as_failed(tmp_path):
    class BrokenLLM:
        async def chat(self, messages, tools=None):
            raise RuntimeError("provider is down")

    j = _journal(tmp_path)
    director = Director(llm=BrokenLLM(), journal=j)
    with pytest.raises(RuntimeError):
        await director.handle(Task(content="проверь диск", chat_id="c1"))

    outcome, problems = _episode_of(tmp_path)
    assert outcome == "failed"
    assert problems == ["задача оборвалась: RuntimeError: provider is down"]


def test_search_returns_episode_and_skips_it_for_old_tasks(tmp_path):
    path = str(tmp_path / "tasks.db")
    j = TaskJournal(path)
    j.record(task_id="t1", chat_id="c1", intent="перезапусти панель", agents=[],
             tool_seq=[], iterations=2, success=True, summary="Не перезапустил.",
             outcome="partial", problems=["отказ в подтверждении: docker_restart: container=panel"])
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, ts, chat_id, intent, agent, tool_seq, iterations, success, "
            "summary) VALUES ('old', ?, 'c1', 'проверь сертификат', '', '[]', 1, 1, 'Ок.')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute("INSERT INTO tasks_fts (id, intent, summary) "
                     "VALUES ('old', 'проверь сертификат', 'Ок.')")

    found = j.search("перезапусти панель")[0]
    assert found["outcome"] == "partial"
    assert found["problems"] == ["отказ в подтверждении: docker_restart: container=panel"]
    # задача старше Ш6: пустого эпизода не выдумываем
    assert set(j.search("сертификат")[0]) == {"intent", "summary", "skills"}
