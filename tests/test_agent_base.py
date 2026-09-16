import json
from unittest.mock import AsyncMock
from pydantic import BaseModel
from app.agents.base import Agent, clamp_output
from app.config import settings
from app.agents.messages import Task, Result, ConfirmationRequest, Decision
from app.tools.base import Tool, Safety
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction


class P(BaseModel):
    x: str


async def _fn(x: str) -> dict:
    return {"got": x}


def make_tool():
    return Tool(name="echo", description="d", params_model=P, fn=_fn, safety=Safety.SAFE)


class FakeLLM:
    def __init__(self, responses):
        self._r = responses
        self.last_messages = None

    async def chat(self, messages, tools=None):
        self.last_messages = list(messages)
        return self._r.pop(0)


async def test_agent_runs_safe_tool_then_answers():
    tool = make_tool()
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))])
    final = ChoiceMessage(content="result: hi", tool_calls=None)
    llm = FakeLLM([tc, final])
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm)
    res = await agent.handle(Task(content="do it"))
    assert res.success is True
    assert "result" in res.content


async def test_text_written_alongside_tool_call_is_not_lost():
    """Модель пишет отчёт в ходе с инструментом, а последним ходом — «отчёт выше»."""
    tool = make_tool()
    with_report = ChoiceMessage(
        content="Полный отчёт по схемам",
        tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))],
    )
    final = ChoiceMessage(content="Схемы сняты — отчёт выше.", tool_calls=None)
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=FakeLLM([with_report, final]))
    res = await agent.handle(Task(content="сравни схемы"))
    assert "Полный отчёт по схемам" in res.content
    assert "отчёт выше" in res.content


async def test_dangerous_rejected():
    class Q(BaseModel):
        c: str

    async def _danger(c: str) -> dict:
        return {"done": c}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class NoGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.REJECTED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="rejected", tool_calls=None)
    llm = FakeLLM([tc, final])
    agent = Agent(name="t", system_prompt="sys", tools=[dt], llm=llm, gateway=NoGateway())
    res = await agent.handle(Task(content="restart bot"))
    assert res.success is True
    assert "rejected" in res.content


async def test_dangerous_action_is_audited(tmp_path, monkeypatch):
    from app import audit

    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))

    class Q(BaseModel):
        c: str

    async def _danger(c: str) -> dict:
        return {"returncode": 0, "done": c}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class YesGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.APPROVED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="готово", tool_calls=None)
    llm = FakeLLM([tc, final])
    agent = Agent(name="hostadmin", system_prompt="sys", tools=[dt], llm=llm, gateway=YesGateway())
    await agent.handle(Task(content="restart bot"))
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "hostadmin"
    assert rec["tool"] == "restart"
    assert rec["decision"] == "approved"
    assert rec["result"]["returncode"] == 0


async def test_auto_approved_call_runs_and_is_audited_as_such(tmp_path, monkeypatch):
    from app import audit

    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))

    class Q(BaseModel):
        c: str

    ran = []

    async def _danger(c: str) -> dict:
        ran.append(c)
        return {"returncode": 0}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class GrantedGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            return Decision.AUTO_APPROVED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    agent = Agent(name="t", system_prompt="sys", tools=[dt],
                  llm=FakeLLM([tc, ChoiceMessage(content="ok", tool_calls=None)]), gateway=GrantedGateway())
    await agent.handle(Task(content="restart bot"))
    assert ran == ["bot"]
    assert json.loads(path.read_text(encoding="utf-8"))["decision"] == "auto-approved"


async def test_failed_precheck_never_reaches_confirmation():
    class Q(BaseModel):
        container: str

    ran, asked = [], []

    async def _danger(container: str) -> dict:
        ran.append(container)
        return {}

    async def _missing(args: dict) -> str | None:
        return f"контейнер {args['container']} не найден"

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger,
              safety=Safety.DANGEROUS, precheck=_missing)

    class SpyGateway:
        async def request(self, req: ConfirmationRequest) -> Decision:
            asked.append(req)
            return Decision.APPROVED

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"container": "nope"})))])
    llm = FakeLLM([tc, ChoiceMessage(content="нет такого", tool_calls=None)])
    agent = Agent(name="t", system_prompt="sys", tools=[dt], llm=llm, gateway=SpyGateway())
    await agent.handle(Task(content="restart nope"))
    assert asked == [] and ran == []


# ---------- подтверждается и исполняется один снимок (аудит F04/F05) ----------

class HostCmd(BaseModel):
    host: str
    command: list[str]
    options: dict = {}


def _dangerous_call(args: dict, id_: str = "c1") -> ChoiceMessage:
    return ChoiceMessage(content=None, tool_calls=[ToolCall(
        id=id_, function=ToolCallFunction(name="ssh_exec", arguments=json.dumps(args)))])


async def _run_ssh(monkeypatch, tmp_path, args, gateway, fn=None):
    from app import audit

    monkeypatch.setattr(audit.settings, "audit_trail_path", str(tmp_path / "audit.jsonl"))
    executed = []

    async def _exec(host: str, command: list[str], options: dict) -> dict:
        executed.append({"host": host, "command": list(command), "options": dict(options)})
        if fn:
            fn(command, options)
        return {"returncode": 0}

    tool = Tool("ssh_exec", "d", HostCmd, _exec, Safety.DANGEROUS)
    llm = FakeLLM([_dangerous_call(args), ChoiceMessage(content="готово", tool_calls=None)])
    task = Task(content="перезапусти", run_id="root-run")
    await Agent(name="ssh", system_prompt="sys", tools=[tool], llm=llm, gateway=gateway).handle(task)
    return executed, tmp_path / "audit.jsonl"


class MutatingGateway:
    """Шлюз, который после получения запроса подменяет цель и вложенные аргументы."""

    def __init__(self):
        self.requests = []

    async def request(self, req):
        self.requests.append(req.model_copy(deep=True))
        req.args["host"] = "node-b"
        req.args["command"].append("--force")
        req.args["options"]["x"] = 1
        return Decision.APPROVED


async def test_changing_request_args_does_not_change_execution(monkeypatch, tmp_path):
    gw = MutatingGateway()
    args = {"host": "node-a", "command": ["systemctl", "restart", "nginx"], "_intent": "Перезапущу."}
    executed, _ = await _run_ssh(monkeypatch, tmp_path, args, gw)
    assert executed == [{"host": "node-a", "command": ["systemctl", "restart", "nginx"], "options": {}}]
    assert gw.requests[0].args == executed[0]  # показано ровно то, что исполнено


async def test_tool_mutating_its_args_does_not_change_audited_snapshot(monkeypatch, tmp_path):
    class Yes:
        async def request(self, req):
            return Decision.APPROVED

    def mutate(command, options):
        command.append("rm -rf /")
        options["evil"] = True

    args = {"host": "node-a", "command": ["uptime"], "options": {"a": 1}}
    _, audit_path = await _run_ssh(monkeypatch, tmp_path, args, Yes(), fn=mutate)
    rec = json.loads(audit_path.read_text(encoding="utf-8"))
    assert rec["args"] == {"host": "node-a", "command": ["uptime"], "options": {"a": 1}}


async def test_invalid_args_never_reach_confirmation(monkeypatch, tmp_path):
    gw = MutatingGateway()
    executed, _ = await _run_ssh(monkeypatch, tmp_path, {"host": "node-a"}, gw)
    assert gw.requests == [] and executed == []


async def test_request_identity_is_set_by_code(monkeypatch, tmp_path):
    gw = MutatingGateway()
    args = {"host": "node-a", "command": ["uptime"], "_intent": "Посмотрю."}
    await _run_ssh(monkeypatch, tmp_path, args, gw)
    (req,) = gw.requests
    assert req.run_id == "root-run"
    assert req.tool_call_id == "c1"
    assert req.agent_id.startswith("ssh#")
    assert req.reason == "Посмотрю."
    assert "_intent" not in req.args


async def test_max_iterations_keeps_partial_answer(monkeypatch):
    from app.agents import base as base_mod

    monkeypatch.setattr(base_mod.settings, "agent_max_iterations", 2)
    tool = make_tool()

    def _tool_msg(i):
        return ChoiceMessage(
            content=f"работаю, шаг {i}",
            tool_calls=[ToolCall(id=f"c{i}", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))],
        )

    llm = FakeLLM([_tool_msg(1), _tool_msg(2)])
    mem = FakeMemory()
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm, memory=mem)
    res = await agent.handle(Task(content="do it"))
    assert res.success is False
    assert "работаю, шаг 2" in res.content
    assert "лимит итераций" in res.content
    assert mem.items[-1]["content"] == res.content


class FakeMemory:
    def __init__(self):
        self.items = []

    def load(self, chat_id):
        return list(self.items)

    def append(self, chat_id, role, content):
        self.items.append({"role": role, "content": content})


async def test_agent_saves_final_turn_to_memory():
    mem = FakeMemory()
    final = ChoiceMessage(content="готово", tool_calls=None)
    llm = FakeLLM([final])
    agent = Agent(name="d", system_prompt="sys", tools=[], llm=llm, memory=mem)
    await agent.handle(Task(content="сделай"))
    assert mem.items == [
        {"role": "user", "content": "сделай"},
        {"role": "assistant", "content": "готово"},
    ]


async def test_agent_loads_history_into_prompt():
    mem = FakeMemory()
    mem.append("c1", "user", "прошлый вопрос")
    mem.append("c1", "assistant", "прошлый ответ")
    final = ChoiceMessage(content="ок", tool_calls=None)
    llm = FakeLLM([final])
    agent = Agent(name="d", system_prompt="sys", tools=[], llm=llm, memory=mem)
    await agent.handle(Task(content="новый"))
    contents = [m["content"] for m in llm.last_messages]
    assert "прошлый вопрос" in contents
    assert "прошлый ответ" in contents
    assert contents[0] == "sys"
    assert contents[-1] == "новый"


async def test_agent_without_memory_unchanged():
    final = ChoiceMessage(content="ответ", tool_calls=None)
    llm = FakeLLM([final])
    agent = Agent(name="t", system_prompt="sys", tools=[], llm=llm)
    res = await agent.handle(Task(content="q"))
    assert res.content == "ответ"


async def test_safe_tools_run_in_parallel():
    import asyncio

    running = 0
    peak = 0

    async def _slow(x: str) -> dict:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return {"got": x}

    tool = Tool(name="slow", description="d", params_model=P, fn=_slow, safety=Safety.SAFE)
    calls = ChoiceMessage(content=None, tool_calls=[
        ToolCall(id=f"c{i}", function=ToolCallFunction(name="slow", arguments=json.dumps({"x": str(i)})))
        for i in range(3)
    ])
    final = ChoiceMessage(content="done", tool_calls=None)
    llm = FakeLLM([calls, final])
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm)
    res = await agent.handle(Task(content="do"))
    assert res.success is True
    assert peak == 3
    # порядок ответов совпадает с порядком tool_calls
    tool_msgs = [m for m in llm.last_messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1", "c2"]


async def test_bad_escape_in_args_recovered():
    from app.agents.base import parse_args

    # то, что реально прислала LLM: regex с \d внутри строки JSON
    raw = r'{"x": "openssl x509 -enddate | grep -oP \d{4}"}'
    assert parse_args(raw)["x"].endswith(r"\d{4}")

    tool = make_tool()
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments=raw))])
    final = ChoiceMessage(content="ok", tool_calls=None)
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=FakeLLM([tc, final]))
    res = await agent.handle(Task(content="certs"))
    assert res.success is True


async def test_unparsable_args_reported_to_llm():
    tool = make_tool()
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments="{broken"))])
    final = ChoiceMessage(content="ok", tool_calls=None)
    llm = FakeLLM([tc, final])
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm)
    res = await agent.handle(Task(content="x"))
    assert res.success is True
    assert "invalid tool arguments" in llm.last_messages[-1]["content"]


async def test_reasoning_content_returned_to_model():
    """Thinking-модель отвечает 400, если её размышления не вернуть в истории."""
    tool = make_tool()
    tc = ChoiceMessage(
        content=None,
        tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))],
        reasoning_content="думаю",
    )
    llm = FakeLLM([tc, ChoiceMessage(content="ok", tool_calls=None)])
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm)
    await agent.handle(Task(content="do it"))
    assistant = next(m for m in llm.last_messages if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "думаю"


def test_clamp_output_leaves_short_text_alone():
    assert clamp_output("короткий вывод", 100) == "короткий вывод"


def test_clamp_output_disabled_by_zero():
    assert clamp_output("x" * 1000, 0) == "x" * 1000


def test_clamp_output_keeps_head_and_tail():
    text = "НАЧАЛО" + "m" * 1000 + "КОНЕЦ"
    out = clamp_output(text, 100)
    assert out.startswith("НАЧАЛО")
    assert out.endswith("КОНЕЦ")
    assert "вырезано 911 символов" in out


async def test_long_tool_output_reaches_model_clamped(monkeypatch):
    """Логи на мегабайт не должны уезжать в контекст целиком."""
    long_line = "L" * 5000

    class Big(BaseModel):
        pass

    async def _big() -> str:
        return long_line

    tool = Tool(name="big", description="d", params_model=Big, fn=_big, safety=Safety.SAFE)
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="big", arguments="{}"))])
    final = ChoiceMessage(content="готово", tool_calls=None)
    llm = FakeLLM([tc, final])
    monkeypatch.setattr(settings, "tool_output_max_chars", 200)
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm)
    await agent.handle(Task(content="дай логи"))
    tool_msg = next(m for m in llm.last_messages if m["role"] == "tool")
    assert len(tool_msg["content"]) < 300
    assert "вырезано" in tool_msg["content"]
