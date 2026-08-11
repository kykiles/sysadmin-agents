import json

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
    assert "docker: compose_path, engine_version" in idx
    assert "global: timezone" in idx
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


async def test_fact_written_after_an_untrusted_spawn_is_flagged(tmp_path):
    """Недоверенный текст возвращается в контекст Директора: всё, что он запишет
    в память по итогам такой задачи, должен увидеть человек."""
    facts.init_store(str(tmp_path / "f.db"))
    lib = {"search": Skill(name="search", description="ищет в вебе",
                           instructions="## поиск", tools=[], untrusted=True)}
    llm = FakeLLM([
        _call("spawn", {"role": "х", "skills": ["search"], "task": "найди asn"}),
        ChoiceMessage(content="AS123", tool_calls=None),                     # агент
        _call("remember_fact", {"scope": "net", "key": "asn", "value": "AS123"}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=lib)
    await d.handle(Task(content="узнай asn"))

    assert [f["key"] for f in facts.get_store().tainted()] == ["asn"]


async def test_fact_from_an_ordinary_task_is_not_flagged(tmp_path):
    facts.init_store(str(tmp_path / "f.db"))
    llm = FakeLLM([
        _call("remember_fact", {"scope": "net", "key": "asn", "value": "AS123"}),
        ChoiceMessage(content="готово", tool_calls=None),
    ])
    d = Director(llm=llm, skills=_skill())
    await d.handle(Task(content="запомни"))

    assert facts.get_store().tainted() == []
