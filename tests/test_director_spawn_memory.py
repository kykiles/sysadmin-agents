import asyncio
import json

import pytest

from app.agents.director import Director, _memory_index
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from agent_memory.facts import KnowledgeStore
from app.skills.loader import Skill
from app.tools.base import Tool, Safety
from pydantic import BaseModel


class FakeLLM:
    def __init__(self, responses):
        self._r = responses
        self.seen: list[list[dict]] = []
        self.seen_tools: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.seen.append(messages)
        self.seen_tools.append(tools or [])
        return self._r.pop(0)


class EchoParams(BaseModel):
    text: str


async def _echo(text: str) -> dict:
    return {"echo": text}


def _skill() -> dict[str, Skill]:
    tool = Tool("echo", "echo back", EchoParams, _echo, Safety.SAFE)
    return {"writer": Skill(name="writer", description="пишет тексты",
                            instructions="## Навык: письмо", tools=[tool])}


def _call(name: str, args: dict) -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1", function=ToolCallFunction(name=name, arguments=json.dumps(args)))])


async def test_spawn_runs_temporary_agent(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    # Директор спавнит агента, тот вызывает echo и отвечает.
    llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши пост"}),
        _call("echo", {"text": "пост"}),
        ChoiceMessage(content="готово", tool_calls=None),
        ChoiceMessage(content="Пост готов.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    res = await d.handle(Task(content="сделай пост"))

    assert res.content == "Пост готов."
    assert d._agents_used == ["spawned:writer"]
    assert "echo" in d._sub_trace
    # У временного агента свой промпт из SKILL.md и никакой истории диалога.
    assert "## Навык: письмо" in llm.seen[1][0]["content"]


async def test_spawned_agent_runs_on_agent_llm(tmp_path):
    """Директор и временные агенты могут сидеть на разных моделях."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    director_llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши"}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    agent_llm = FakeLLM([ChoiceMessage(content="написал", tool_calls=None)])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill(), facts=store)
    res = await d.handle(Task(content="пост"))

    assert res.content == "Готово."
    assert len(director_llm.seen) == 2
    assert all(m[0]["content"].startswith("Ты — Директор") for m in director_llm.seen)
    (sub,) = agent_llm.seen
    assert "## Навык: письмо" in sub[0]["content"]


async def test_spawn_dedupes_tools_shared_by_skills(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    # Два навыка с одноимённым инструментом: шлюз на дубль имени отвечает 400.
    lib = _skill()
    lib["editor"] = Skill(name="editor", description="правит", instructions="## правка",
                          tools=[Tool("echo", "echo back", EchoParams, _echo, Safety.SAFE)])
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["writer", "editor"], "task": "t"}),
        ChoiceMessage(content="готово", tool_calls=None),
        ChoiceMessage(content="ок", tool_calls=None),
    ])
    d = Director(llm=llm, skills=lib, facts=store)
    await d.handle(Task(content="сделай"))

    names = [t["function"]["name"] for t in llm.seen_tools[1]]
    assert names == ["echo"]


async def _spawn_tools(tmp_path, lib, skills) -> dict:
    store = KnowledgeStore(str(tmp_path / "f.db"))
    llm = FakeLLM([ChoiceMessage(content="готово", tool_calls=None)])
    d = Director(llm=llm, skills=lib, facts=store)
    spawn = next(t for t in d.tools if t.name == "spawn")
    return json.loads(await spawn.execute({"role": "х", "skills": skills, "task": "t"}))


async def test_spawn_rejects_same_name_with_different_code(tmp_path):
    """Аудит F07: из двух одноимённых реализаций dict молча оставлял последнюю."""
    async def _other(text: str) -> dict:
        return {"other": text}

    lib = _skill()
    lib["impostor"] = Skill(name="impostor", description="д", instructions="и",
                            tools=[Tool("echo", "echo back", EchoParams, _other, Safety.SAFE)])
    out = await _spawn_tools(tmp_path, lib, ["writer", "impostor"])
    assert "echo" in out["error"]


async def test_spawn_rejects_two_servers_with_one_tool_name(tmp_path):
    def mcp_skill(name, server):
        tool = Tool("search", "s", EchoParams, _echo, Safety.SAFE, remote=(server, "search"))
        return Skill(name=name, description="д", instructions="и", tools=[tool])

    lib = {"a": mcp_skill("a", "a:mcp-a.example"), "b": mcp_skill("b", "b:mcp-b.example")}
    out = await _spawn_tools(tmp_path, lib, ["a", "b"])
    assert "search" in out["error"]


async def test_spawn_rejects_skill_tool_shadowing_host_query(tmp_path):
    from app.skills.readonly import HostAccess

    lib = {
        "host": Skill(name="host", description="х", instructions="и", tools=[],
                      access=HostAccess(binaries=frozenset({"df"}))),
        "fake": Skill(name="fake", description="ф", instructions="и",
                      tools=[Tool("host_query", "q", EchoParams, _echo, Safety.SAFE)]),
    }
    out = await _spawn_tools(tmp_path, lib, ["host", "fake"])
    assert "host_query" in out["error"]


async def test_spawn_rejects_unknown_skill(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["нетакого"], "task": "t"}),
        ChoiceMessage(content="навыка нет", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    await d.handle(Task(content="сделай"))

    tool_reply = json.loads(llm.seen[1][-1]["content"])
    assert "нетакого" in tool_reply["error"]
    assert d._agents_used == []


async def test_spawned_agents_never_get_memory_tools(tmp_path):
    """Память принадлежит Директору. Держится не фильтром библиотеки, а тем, что
    инструменты памяти приходят из ядра и в скилах их нет вовсе."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["writer"], "task": "запомни хост"}),
        ChoiceMessage(content="сделал", tool_calls=None),   # ответ спавнутого агента
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    await d.handle(Task(content="сделай"))

    memory_tools = {"recall_facts", "remember_fact"}
    assert memory_tools <= {t.name for t in d.tools}
    # второй вызов LLM — это спавнутый агент со своим набором инструментов
    assert not memory_tools & {t["function"]["name"] for t in llm.seen_tools[1]}


def test_memory_index_lists_keys_not_values(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("docker", "compose_path", "/opt/app")
    store.remember("docker", "engine_version", "27.1")
    store.remember("global", "timezone", "UTC")

    idx = _memory_index(store)
    assert "- docker:" in idx
    assert "compose_path" in idx and "engine_version" in idx
    assert "- global:" in idx and "timezone" in idx
    assert "/opt/app" not in idx  # значения в промпт не попадают


def test_memory_index_marks_lessons_and_bans(tmp_path):
    """Урок и запрет — не факты об инфраструктуре: в оглавлении они помечены словом."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("deploy", "check_backup", "перед миграцией снять бэкап", "lesson")
    store.remember("deploy", "no_restart_all", "рестарт всего стека не помогает",
                   "negative_rule")
    store.remember("deploy", "compose_path", "/opt/app")

    idx = _memory_index(store)

    assert "check_backup (урок)" in idx
    assert "no_restart_all (не делать)" in idx
    assert "compose_path\n" in idx or idx.endswith("compose_path")


async def test_spawned_agent_gets_one_host_query_with_union_scope(tmp_path):
    """tls + security → один host_query, видящий бинарники обоих навыков.

    Раньше это были два инструмента с разными именами; после унификации имени
    коллизия в uniq молча оставила бы скоуп только первого навыка.
    """
    from skills.security.tools import ACCESS as SEC
    from skills.tls.tools import ACCESS as TLS

    store = KnowledgeStore(str(tmp_path / "f.db"))
    lib = {
        "tls": Skill(name="tls", description="сертификаты", instructions="п", tools=[], access=TLS),
        "security": Skill(name="security", description="аудит", instructions="п", tools=[], access=SEC),
    }
    llm = FakeLLM([
        _call("spawn", {"role": "аудитор", "skills": ["tls", "security"], "task": "проверь"}),
        ChoiceMessage(content="проверено", tool_calls=None),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=lib, facts=store)
    await d.handle(Task(content="аудит сертификатов"))

    sub_tools = {t["function"]["name"] for t in llm.seen_tools[1]}
    assert "host_query" in sub_tools
    assert not {"tls_query", "sec_query"} & sub_tools  # старых имён больше нет

    (schema,) = [t for t in llm.seen_tools[1] if t["function"]["name"] == "host_query"]
    description = schema["function"]["description"]
    assert "certbot" in description and "fail2ban-client" in description


async def test_spawned_agent_without_host_skills_gets_no_host_query(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши"}),
        ChoiceMessage(content="написал", tool_calls=None),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    await d.handle(Task(content="пост"))

    sub_tools = {t["function"]["name"] for t in llm.seen_tools[1]}
    assert sub_tools == {"echo"}


async def test_fact_written_after_an_untrusted_spawn_goes_to_quarantine(tmp_path):
    """Недоверенный текст возвращается в контекст Директора: что он запишет по итогам
    такой задачи, в активную память не попадает до одобрения владельцем (аудит F09)."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("net", "asn", "AS100")
    lib = {"search": Skill(name="search", description="ищет в вебе",
                           instructions="## поиск", tools=[], untrusted=True)}
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["search"], "task": "найди asn"}),
        ChoiceMessage(content="AS666", tool_calls=None),                     # агент
        # объявить факт проверенным модель не может: таких параметров нет
        _call("remember_fact", {"scope": "net", "key": "asn", "value": "AS666",
                                "verified": True, "tainted": False}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=lib, facts=store)
    await d.handle(Task(content="узнай asn", run_id="run-7"))

    (p,) = store.proposals()
    assert (p["value"], p["run_id"], p["tool"], p["source"]) == (
        "AS666", "run-7", "remember_fact", "spawn:search")
    assert store.recall(scope="net")[0]["value"] == "AS100"
    replies = [json.loads(m["content"]) for m in llm.seen[-1] if m.get("role") == "tool"]
    assert "proposed" in replies[-1] and "remembered" not in replies[-1]


async def test_fact_from_an_ordinary_task_is_active(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("remember_fact", {"scope": "net", "key": "asn", "value": "AS123"}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    await d.handle(Task(content="запомни", run_id="run-9"))

    assert store.proposals() == []
    assert store.recall(scope="net")[0]["value"] == "AS123"
    # откуда факт взялся: канал записи и задача, по которой поднимается транскрипт
    with store._connect() as conn:
        assert conn.execute("SELECT origin, task_id FROM facts").fetchone() == ("director", "run-9")


async def _remember_over(store: KnowledgeStore, value: str) -> dict:
    llm = FakeLLM([
        _call("remember_fact", {"scope": "bot", "key": "cabinet_db", "value": value}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    await d.handle(Task(content="активность glowshine-app", run_id="run-11"))
    return [json.loads(m["content"]) for m in llm.seen[-1] if m.get("role") == "tool"][-1]


@pytest.mark.parametrize("origin", ["owner", "consolidation", "quarantine"])
async def test_director_does_not_overwrite_human_fact(tmp_path, origin):
    """23.09: Директор записал бедную версию bot/cabinet_db поверх версии владельца."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("bot", "cabinet_db", "схема payments, pg_read", origin=origin)

    reply = await _remember_over(store, "контейнер, 4 таблицы")

    assert store.recall(scope="bot")[0]["value"] == "схема payments, pg_read"
    (p,) = store.proposals()
    assert (p["value"], p["run_id"], p["current"]) == (
        "контейнер, 4 таблицы", "run-11", "схема payments, pg_read")
    assert "proposed" in reply and "remembered" not in reply
    assert reply["current"] == "схема payments, pg_read"


async def test_same_value_over_human_fact_confirms_it(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("bot", "cabinet_db", "схема payments", origin="owner")

    reply = await _remember_over(store, "схема  payments")

    assert "remembered" in reply and store.proposals() == []
    with store._connect() as conn:
        assert conn.execute("SELECT confirmed, origin FROM facts").fetchall() == [(1, "owner")]


async def test_director_fact_is_overwritten_as_before(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("bot", "cabinet_db", "старое")

    reply = await _remember_over(store, "новое")

    assert "remembered" in reply and store.proposals() == []
    assert store.recall(scope="bot")[0]["value"] == "новое"


def test_memory_index_collapses_tail_when_budget_spent():
    """Оглавление не растёт вместе с базой: хвост схлопывается в одну строку."""
    from app.agents.director import _render_index

    area = {"scope": "host", "facts": [
        {"key": f"key_{i}", "description": "довольно длинное описание факта",
         "kind": "stable"} for i in range(50)
    ]}
    lines = _render_index([area], token_budget=40)

    assert lines[-1].startswith("  - ... ещё ")
    assert sum(len(l) // 4 + 1 for l in lines[:-1]) <= 40


# ---------- подтверждения специалистов: отдельный запрос на каждый вызов (аудит F04) ----------

class HostParams(BaseModel):
    host: str


class RoutingLLM:
    """Отвечает по системному промпту: Директор спавнит двоих, каждый просит перезапуск."""

    async def chat(self, messages, tools=None):
        system = messages[0]["content"]
        done = any(m.get("role") == "tool" for m in messages)
        if system.startswith("Ты — Директор"):
            if done:
                return ChoiceMessage(content="Итог.", tool_calls=None)
            return ChoiceMessage(content=None, tool_calls=[
                ToolCall(id=f"s{n}", function=ToolCallFunction(name="spawn", arguments=json.dumps(
                    {"role": f"агент {n}", "skills": ["ops"], "task": "перезапусти"})))
                for n in ("A", "B")
            ])
        if done:
            return ChoiceMessage(content="сделано", tool_calls=None)
        host = "node-a" if "агент A" in system else "node-b"
        return _call("restart", {"host": host, "_intent": f"Перезапущу {host}."})


def _ops_library(executed: list) -> dict[str, Skill]:
    async def _restart(host: str) -> dict:
        executed.append(host)
        return {"returncode": 0}

    return {"ops": Skill(name="ops", description="ops", instructions="## ops",
                         tools=[Tool("restart", "restart", HostParams, _restart, Safety.DANGEROUS)])}


def _telegram_gateway():
    import itertools
    from unittest.mock import AsyncMock, MagicMock
    from app.bot.gateway import TelegramConfirmationGateway

    ids = itertools.count(100)
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda *a, **k: MagicMock(message_id=next(ids)))
    gw = TelegramConfirmationGateway(bot, chat_id=1, timeout=30)
    requests = []
    original = gw.request

    async def spy(req):
        requests.append(req)
        return await original(req)

    gw.request = spy
    return gw, bot, requests


async def _wait_pending(gw, n):
    # По времени, а не по числу тиков: Директор ходит в поток (оглавление памяти),
    # и под нагрузкой 200 тиков не хватало.
    for _ in range(500):
        if len(gw._pending) == n:
            return
        await asyncio.sleep(0.01)


async def test_two_specialists_get_separate_confirmations(tmp_path):
    from app.agents.messages import Decision

    store = KnowledgeStore(str(tmp_path / "f.db"))
    executed: list[str] = []
    gw, bot, requests = _telegram_gateway()
    d = Director(llm=RoutingLLM(), gateway=gw, skills=_ops_library(executed), facts=store)
    root = Task(content="перезапусти обе ноды")
    run = asyncio.create_task(d.handle(root))
    await _wait_pending(gw, 2)

    by_host = {}
    for call in bot.send_message.call_args_list:
        rid = call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
        host = "node-a" if "node-a" in call.args[1] else "node-b"
        by_host[host] = (rid, gw._pending[rid].message_id)
    assert set(by_host) == {"node-a", "node-b"}
    rid_a, msg_a = by_host["node-a"]
    rid_b, msg_b = by_host["node-b"]
    assert gw.resolve(rid_a, Decision.APPROVED, user_id=1, chat_id=1, message_id=msg_a)
    assert gw.resolve(rid_b, Decision.REJECTED, user_id=1, chat_id=1, message_id=msg_b)
    await run

    assert executed == ["node-a"]
    assert {r.run_id for r in requests} == {root.id}
    assert len({r.agent_id for r in requests}) == 2
    assert gw._pending == {}


async def test_yes_to_all_covers_run_children_and_ends_with_answer(tmp_path):
    from app.agents.messages import Decision

    store = KnowledgeStore(str(tmp_path / "f.db"))
    executed: list[str] = []
    gw, bot, _requests = _telegram_gateway()
    d = Director(llm=RoutingLLM(), gateway=gw, skills=_ops_library(executed), facts=store)
    root = Task(content="перезапусти обе ноды")
    gw._grants[root.id] = {"restart: host=node-a"}
    run = asyncio.create_task(d.handle(root))
    await _wait_pending(gw, 1)
    (rid, pending), = gw._pending.items()
    assert gw.resolve(rid, Decision.REJECTED, user_id=1, chat_id=1, message_id=pending.message_id)
    await run

    assert executed == ["node-a"]
    assert bot.send_message.await_count == 1
    assert gw._grants == {}


async def test_parent_cancel_clears_child_pending(tmp_path):
    from app.agents.messages import Decision

    store = KnowledgeStore(str(tmp_path / "f.db"))
    executed: list[str] = []
    gw, _bot, _requests = _telegram_gateway()
    d = Director(llm=RoutingLLM(), gateway=gw, skills=_ops_library(executed), facts=store)
    run = asyncio.create_task(d.handle(Task(content="перезапусти")))
    await _wait_pending(gw, 2)
    stale = {rid: p.message_id for rid, p in gw._pending.items()}

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert gw._pending == {}
    for rid, msg_id in stale.items():
        assert not gw.resolve(rid, Decision.APPROVED, user_id=1, chat_id=1, message_id=msg_id)
    assert executed == []


async def test_remember_fact_shows_similar_but_writes_anyway(tmp_path):
    """Дубль под другим ключом Директор увидит в ответе инструмента — но запись
    не блокируем: двухходовка заставила бы его избегать remember_fact."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    store.remember("bot", "dialog_db", "история диалога в /data/dialog.db")
    llm = FakeLLM([
        _call("remember_fact", {"scope": "bot", "key": "history_path",
                                "value": "диалог хранится в /data/dialog.db"}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=store)
    await d.handle(Task(content="запомни"))

    result = next(m for m in llm.seen[-1] if m.get("role") == "tool")
    assert "dialog_db" in result["content"]
    assert {f["key"] for f in store.recall()} == {"dialog_db", "history_path"}


# ---------- TODO-лист: план от Директора, отметки ставит код ----------

def _progress_bot():
    from unittest.mock import AsyncMock, MagicMock
    from app.bot.progress import TelegramProgress

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    bot.edit_message_text = AsyncMock()
    return TelegramProgress(bot, 1), bot


async def test_agent_marks_plan_steps_itself(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    progress, bot = _progress_bot()
    director_llm = FakeLLM([
        _call("plan", {"title": "Пост", "steps": ["Написать пост", "Проверить"]}),
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши", "steps": [1, 2]}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    agent_llm = FakeLLM([
        _call("mark_step", {"step": 1, "status": "done"}),
        ChoiceMessage(content="написал", tool_calls=None),
        # напоминание про пункт 2 — агент снова не отметил
        ChoiceMessage(content="всё написал", tool_calls=None),
    ])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill(),
                 progress=progress, facts=store)
    await d.handle(Task(content="пост"))

    shown = [c.args[0] for c in bot.edit_message_text.await_args_list]
    assert shown == ["<b>Пост</b>\n\n1. Написать пост — <i>в работе</i>\n2. Проверить — <i>в работе</i>",
                     "<b>Пост</b>\n\n<s>1. Написать пост</s>\n2. Проверить — <i>в работе</i>",
                     "<b>Пост</b>\n\n<s>1. Написать пост</s>\n2. Проверить",
                     "<b>Пост</b>\n\n<s>1. Написать пост</s>\n2. Проверить — <i>пропущен</i>"]
    assert progress._boards == {}
    assert "plan" in [t["function"]["name"] for t in director_llm.seen_tools[0]]
    # пункты агента — в его промпте, mark_step — в его инструментах
    (sub,) = agent_llm.seen[:1]
    assert "1. Написать пост\n2. Проверить" in sub[0]["content"]
    assert "mark_step" in [t["function"]["name"] for t in agent_llm.seen_tools[0]]


async def test_agent_that_forgot_marks_gets_one_reminder_turn(tmp_path):
    """Прод 16.09: deepseek-v4-flash сделал работу, но mark_step не вызвал — пункты
    ушли в «пропущен». Один ход только с mark_step; ответ агента не пишется заново."""
    store = KnowledgeStore(str(tmp_path / "f.db"))
    progress, bot = _progress_bot()
    director_llm = FakeLLM([
        _call("plan", {"title": "Пост", "steps": ["Написать пост", "Проверить"]}),
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши", "steps": [1, 2]}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    agent_llm = FakeLLM([
        _call("echo", {"text": "пост"}),
        ChoiceMessage(content="написал и проверил", tool_calls=None),
        ChoiceMessage(content="отмечаю", tool_calls=[
            ToolCall(id="m1", function=ToolCallFunction(
                name="mark_step", arguments=json.dumps({"step": 1, "status": "done"}))),
            ToolCall(id="m2", function=ToolCallFunction(
                name="mark_step", arguments=json.dumps({"step": 2, "status": "done"}))),
            # в ходе-напоминании работа не продолжается
            ToolCall(id="e1", function=ToolCallFunction(
                name="echo", arguments=json.dumps({"text": "ещё"}))),
        ]),
    ])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill(),
                 progress=progress, facts=store)
    await d.handle(Task(content="пост"))

    last = bot.edit_message_text.await_args_list[-1].args[0]
    assert last == "<b>Пост</b>\n\n<s>1. Написать пост</s>\n<s>2. Проверить</s>"
    assert [t["function"]["name"] for t in agent_llm.seen_tools[-1]] == ["mark_step"]
    assert "1, 2" in agent_llm.seen[-1][-1]["content"]
    spawn_out = [m["content"] for m in director_llm.seen[-1] if m["role"] == "tool"][-1]
    assert "написал и проверил" in spawn_out and "отмечаю" not in spawn_out
    assert d._sub_trace.count("echo") == 1


async def test_failed_reminder_keeps_agent_result(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    progress, _ = _progress_bot()
    director_llm = FakeLLM([
        _call("plan", {"title": "Пост", "steps": ["Написать пост"]}),
        _call("spawn", {"role": "к", "skills": ["writer"], "task": "напиши", "steps": [1]}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    # второго ответа нет — ход-напоминание падает
    agent_llm = FakeLLM([ChoiceMessage(content="написал", tool_calls=None)])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill(),
                 progress=progress, facts=store)
    res = await d.handle(Task(content="пост"))

    assert res.content == "Готово."
    spawn_out = [m["content"] for m in director_llm.seen[-1] if m["role"] == "tool"][-1]
    assert "написал" in spawn_out


async def test_no_reminder_when_agent_marked_everything(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    progress, _ = _progress_bot()
    director_llm = FakeLLM([
        _call("plan", {"title": "Пост", "steps": ["Написать пост"]}),
        _call("spawn", {"role": "к", "skills": ["writer"], "task": "напиши", "steps": [1]}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    agent_llm = FakeLLM([
        _call("mark_step", {"step": 1, "status": "done"}),
        ChoiceMessage(content="написал", tool_calls=None),
    ])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill(),
                 progress=progress, facts=store)
    await d.handle(Task(content="пост"))

    assert len(agent_llm.seen) == 2


async def test_spawn_without_steps_is_refused_when_plan_exists(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    progress, _ = _progress_bot()
    llm = FakeLLM([
        _call("plan", {"title": "Пост", "steps": ["Написать"]}),
        _call("spawn", {"role": "к", "skills": ["writer"], "task": "напиши"}),
        _call("spawn", {"role": "к", "skills": ["writer"], "task": "напиши", "steps": [5]}),
        ChoiceMessage(content="Не вышло.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), progress=progress, facts=store)
    await d.handle(Task(content="пост"))

    refusals = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"][1:]
    assert all("укажи steps" in r for r in refusals) and len(refusals) == 2
    assert d._agents_used == []


async def test_spawn_without_plan_needs_no_steps_and_gets_no_mark_step(tmp_path):
    store = KnowledgeStore(str(tmp_path / "f.db"))
    progress, _ = _progress_bot()
    director_llm = FakeLLM([
        _call("spawn", {"role": "к", "skills": ["writer"], "task": "напиши"}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    agent_llm = FakeLLM([ChoiceMessage(content="написал", tool_calls=None)])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill(),
                 progress=progress, facts=store)
    await d.handle(Task(content="пост"))

    assert d._agents_used == ["spawned:writer"]
    assert "mark_step" not in [t["function"]["name"] for t in agent_llm.seen_tools[0]]


def test_no_plan_tool_without_progress():
    d = Director(llm=None, skills=_skill())
    assert "plan" not in [t.name for t in d.tools]
    assert "вызови plan" not in d.system_prompt


def test_director_does_not_ask_for_confirmation_in_text():
    """Живой прогон: Директор просил «подтвердите» текстом, а потом кнопки спрашивали снова."""
    d = Director(llm=None, skills=_skill())
    assert "не проси «подтвердите» текстом" in d.system_prompt


def test_prompt_puts_recall_before_plan():
    """16.09 plan занял слот первого хода: recall_facts упал с 49% задач до 8%.

    Инструкция про чтение памяти была условием без места в потоке, а у plan и
    recall_experience место было («прежде чем», «начни с») — и в 21 задаче из 25
    plan шёл первым вызовом, recall_facts не предшествовал ему ни разу.
    """
    prompt = Director(llm=None, skills=_skill()).system_prompt
    assert "до plan и до spawn" in prompt
    # чтение памяти описано раньше, чем её ведение: порядок в промпте и есть подсказка
    assert prompt.index("recall_facts") < prompt.index("remember_fact")


async def test_spawn_result_reminds_to_remember_what_agent_found(tmp_path):
    """22.09: агенты находили контейнер базы, а remember_fact не звался ни разу за 20 задач."""
    llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши пост"}),
        ChoiceMessage(content="готово", tool_calls=None),
        ChoiceMessage(content="Пост готов.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill(), facts=KnowledgeStore(str(tmp_path / "f.db")))
    await d.handle(Task(content="сделай пост"))

    spawned = json.loads(llm.seen[-1][-1]["content"])
    assert spawned["result"] == "готово"
    assert "remember_fact" in spawned["note"]


def test_prompt_says_when_to_remember_before_warning():
    prompt = Director(llm=None, skills=_skill()).system_prompt
    assert prompt.index("Когда писать через remember_fact") < prompt.index("Разовые находки")
