import asyncio
import json

import pytest

from app.agents.director import Director, _memory_index
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.memory import facts
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
    facts.init_store(str(tmp_path / "f.db"))
    # Директор спавнит агента, тот вызывает echo и отвечает.
    llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши пост"}),
        _call("echo", {"text": "пост"}),
        ChoiceMessage(content="готово", tool_calls=None),
        ChoiceMessage(content="Пост готов.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    res = await d.handle(Task(content="сделай пост"))

    assert res.content == "Пост готов."
    assert d._agents_used == ["spawned:writer"]
    assert "echo" in d._sub_trace
    # У временного агента свой промпт из SKILL.md и никакой истории диалога.
    assert "## Навык: письмо" in llm.seen[1][0]["content"]


async def test_spawned_agent_runs_on_agent_llm(tmp_path):
    """Директор и временные агенты могут сидеть на разных моделях."""
    facts.init_store(str(tmp_path / "f.db"))
    director_llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши"}),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    agent_llm = FakeLLM([ChoiceMessage(content="написал", tool_calls=None)])
    d = Director(llm=director_llm, agent_llm=agent_llm, skills=_skill())
    res = await d.handle(Task(content="пост"))

    assert res.content == "Готово."
    assert len(director_llm.seen) == 2
    assert all(m[0]["content"].startswith("Ты — Директор") for m in director_llm.seen)
    (sub,) = agent_llm.seen
    assert "## Навык: письмо" in sub[0]["content"]


async def test_spawn_dedupes_tools_shared_by_skills(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    # Два навыка с одноимённым инструментом: шлюз на дубль имени отвечает 400.
    lib = _skill()
    lib["editor"] = Skill(name="editor", description="правит", instructions="## правка",
                          tools=[Tool("echo", "echo back", EchoParams, _echo, Safety.SAFE)])
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["writer", "editor"], "task": "t"}),
        ChoiceMessage(content="готово", tool_calls=None),
        ChoiceMessage(content="ок", tool_calls=None),
    ])
    d = Director(llm=llm, skills=lib)
    await d.handle(Task(content="сделай"))

    names = [t["function"]["name"] for t in llm.seen_tools[1]]
    assert names == ["echo"]


async def _spawn_tools(tmp_path, lib, skills) -> dict:
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([ChoiceMessage(content="готово", tool_calls=None)])
    d = Director(llm=llm, skills=lib)
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
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["нетакого"], "task": "t"}),
        ChoiceMessage(content="навыка нет", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    await d.handle(Task(content="сделай"))

    tool_reply = json.loads(llm.seen[1][-1]["content"])
    assert "нетакого" in tool_reply["error"]
    assert d._agents_used == []


async def test_spawned_agents_never_get_memory_tools(tmp_path):
    """Память принадлежит Директору. Держится не фильтром библиотеки, а тем, что
    инструменты памяти приходят из ядра и в скилах их нет вовсе."""
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["writer"], "task": "запомни хост"}),
        ChoiceMessage(content="сделал", tool_calls=None),   # ответ спавнутого агента
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    await d.handle(Task(content="сделай"))

    memory_tools = {"recall_facts", "remember_fact"}
    assert memory_tools <= {t.name for t in d.tools}
    # второй вызов LLM — это спавнутый агент со своим набором инструментов
    assert not memory_tools & {t["function"]["name"] for t in llm.seen_tools[1]}


def test_memory_index_lists_keys_not_values(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    store = facts.get_store()
    store.remember("docker", "compose_path", "/opt/app")
    store.remember("docker", "engine_version", "27.1")
    store.remember("global", "timezone", "UTC")

    idx = _memory_index()
    assert "- docker:" in idx
    assert "compose_path" in idx and "engine_version" in idx
    assert "- global:" in idx and "timezone" in idx
    assert "/opt/app" not in idx  # значения в промпт не попадают


async def test_spawned_agent_gets_one_host_query_with_union_scope(tmp_path):
    """tls + security → один host_query, видящий бинарники обоих навыков.

    Раньше это были два инструмента с разными именами; после унификации имени
    коллизия в uniq молча оставила бы скоуп только первого навыка.
    """
    from skills.security.tools import ACCESS as SEC
    from skills.tls.tools import ACCESS as TLS

    facts.init_store(str(tmp_path / "f.db"))
    lib = {
        "tls": Skill(name="tls", description="сертификаты", instructions="п", tools=[], access=TLS),
        "security": Skill(name="security", description="аудит", instructions="п", tools=[], access=SEC),
    }
    llm = FakeLLM([
        _call("spawn", {"role": "аудитор", "skills": ["tls", "security"], "task": "проверь"}),
        ChoiceMessage(content="проверено", tool_calls=None),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=lib)
    await d.handle(Task(content="аудит сертификатов"))

    sub_tools = {t["function"]["name"] for t in llm.seen_tools[1]}
    assert "host_query" in sub_tools
    assert not {"tls_query", "sec_query"} & sub_tools  # старых имён больше нет

    (schema,) = [t for t in llm.seen_tools[1] if t["function"]["name"] == "host_query"]
    description = schema["function"]["description"]
    assert "certbot" in description and "fail2ban-client" in description


async def test_spawned_agent_without_host_skills_gets_no_host_query(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("spawn", {"role": "копирайтер", "skills": ["writer"], "task": "напиши"}),
        ChoiceMessage(content="написал", tool_calls=None),
        ChoiceMessage(content="Готово.", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    await d.handle(Task(content="пост"))

    sub_tools = {t["function"]["name"] for t in llm.seen_tools[1]}
    assert sub_tools == {"echo"}


async def test_fact_written_after_an_untrusted_spawn_goes_to_quarantine(tmp_path):
    """Недоверенный текст возвращается в контекст Директора: что он запишет по итогам
    такой задачи, в активную память не попадает до одобрения владельцем (аудит F09)."""
    facts.init_store(str(tmp_path / "f.db"))
    store = facts.get_store()
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
    d = Director(llm=llm, skills=lib)
    await d.handle(Task(content="узнай asn", run_id="run-7"))

    (p,) = store.proposals()
    assert (p["value"], p["run_id"], p["tool"], p["source"]) == (
        "AS666", "run-7", "remember_fact", "spawn:search")
    assert store.recall(scope="net")[0]["value"] == "AS100"
    replies = [json.loads(m["content"]) for m in llm.seen[-1] if m.get("role") == "tool"]
    assert "proposed" in replies[-1] and "remembered" not in replies[-1]


async def test_fact_from_an_ordinary_task_is_active(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("remember_fact", {"scope": "net", "key": "asn", "value": "AS123"}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    await d.handle(Task(content="запомни"))

    assert facts.get_store().proposals() == []
    assert facts.get_store().recall(scope="net")[0]["value"] == "AS123"


def test_memory_index_collapses_tail_when_budget_spent():
    """Оглавление не растёт вместе с базой: хвост схлопывается в одну строку."""
    from app.agents.director import _render_index

    area = {"scope": "host", "facts": [
        {"key": f"key_{i}", "description": "довольно длинное описание факта"} for i in range(50)
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
    for _ in range(200):
        await asyncio.sleep(0)
        if len(gw._pending) == n:
            return


async def test_two_specialists_get_separate_confirmations(tmp_path):
    from app.agents.messages import Decision

    facts.init_store(str(tmp_path / "f.db"))
    executed: list[str] = []
    gw, bot, requests = _telegram_gateway()
    d = Director(llm=RoutingLLM(), gateway=gw, skills=_ops_library(executed))
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

    facts.init_store(str(tmp_path / "f.db"))
    executed: list[str] = []
    gw, bot, _requests = _telegram_gateway()
    d = Director(llm=RoutingLLM(), gateway=gw, skills=_ops_library(executed))
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

    facts.init_store(str(tmp_path / "f.db"))
    executed: list[str] = []
    gw, _bot, _requests = _telegram_gateway()
    d = Director(llm=RoutingLLM(), gateway=gw, skills=_ops_library(executed))
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
    facts.init_store(str(tmp_path / "f.db"))
    facts.get_store().remember("bot", "dialog_db", "история диалога в /data/dialog.db")
    llm = FakeLLM([
        _call("remember_fact", {"scope": "bot", "key": "history_path",
                                "value": "диалог хранится в /data/dialog.db"}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    await d.handle(Task(content="запомни"))

    result = next(m for m in llm.seen[-1] if m.get("role") == "tool")
    assert "dialog_db" in result["content"]
    assert {f["key"] for f in facts.get_store().recall()} == {"dialog_db", "history_path"}
