# Агентская система системного администрирования v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Серверная мульти-агентная система (Директор + Главный сисадмин), управляемая через Telegram одним авторизованным пользователем, управляющая Docker-контейнерами хоста.

**Architecture:** Иерархическая делегация через in-memory шину сообщений. Директор (планер/роутер, единственный инструмент `delegate_to`) делегирует технические подзадачи специалистам. Главный сисадмин имеет Docker tools, классифицированные как `safe` (чтение — авто) и `dangerous` (мутации — подтверждение через inline-клавиатуру Telegram, таймаут 5 мин). Лёгкий кастомный цикл LLM↔tools поверх OpenAI-совместимого API (DeepSeek по умолчанию, конфигурируется через env).

**Tech Stack:** Python 3.12 asyncio · aiogram 3.x · `openai` SDK · `aiodocker` · `pydantic` v2 / `pydantic-settings` · `structlog` · `pytest` / `pytest-asyncio` · Docker Compose деплой.

**Spec:** `docs/superpowers/specs/2026-07-08-sysadmin-agents-v1-design.md`

## Global Constraints

- Python 3.12, только asyncio.
- LLM конфиг generic (не захардкожен провайдер): env `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` (defaults: DeepSeek `https://api.deepseek.com` / `deepseek-chat`).
- Авторизация: whitelist из одного `TELEGRAM_USER_ID`.
- `shell_exec` на хосте — НЕ включать в v1. Только Docker API tools. `docker_exec` — dangerous (подтверждение).
- Без БД: состояние в памяти, аудит-лог через `structlog` → stdout + append-only файл на томе.
- Деплой: один сервис в `docker compose`, маунт `/var/run/docker.sock`, long-polling.
- DRY, YAGNI, TDD, частые коммиты. Каждый шаг — одно действие (2–5 мин).

---

## File Structure

```
opencode_agents_system/
  pyproject.toml                    # deps + tool config
  .env.example                      # шаблон env
  Dockerfile
  docker-compose.yml
  app/
    __init__.py
    main.py                         # сборка + startup/shutdown
    config.py                        # pydantic-settings
    logging.py                       # structlog setup
    llm/__init__.py
    llm/client.py                    # generic LLM client (один round-trip)
    tools/__init__.py
    tools/base.py                    # Tool dataclass, Safety, schema gen, execute
    tools/docker.py                  # docker + compose tools (aiodocker + subprocess)
    agents/__init__.py
    agents/messages.py               # Task, Result, ConfirmationRequest
    agents/registry.py               # AgentRegistry + ConfirmationGateway Protocol
    agents/base.py                   # Agent: цикл LLM↔tools + confirmation interception
    agents/director.py               # Director + delegate_to tool
    agents/sysadmin.py               # ChiefSysadmin + docker tools wiring
    bot/__init__.py
    bot/filters.py                   # WhitelistFilter
    bot/keyboards.py                 # approve/reject inline keyboard
    bot/gateway.py                   # TelegramConfirmationGateway (impl ConfirmationGateway)
    bot/handlers.py                  # message→Task, callback_query→confirm
    bot/bot.py                       # Bot+Dispatcher assembly
  tests/
    __init__.py
    conftest.py
    test_config.py
    test_llm_client.py
    test_tools_base.py
    test_tools_docker.py
    test_messages.py
    test_registry.py
    test_agent_base.py
    test_director.py
    test_sysadmin.py
    test_bot_filters.py
    test_bot_gateway.py
    test_bot_handlers.py
```

**Ответственность файлов:** каждый файл — одна ответственность. `llm/client.py` — один round-trip к API (без цикла). Цикл LLM↔tools — в `agents/base.py`. Tools — чистые функции + Pydantic-схемы, без знания про агентов. Шина и подтверждение — в `registry.py` (протокол gateway) + `bot/gateway.py` (реализация). Роли — тонкие классы, собирающие prompt+tools.

---

## Task 1: Scaffolding & Config

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`, `app/config.py`, `app/logging.py`
- Create: `.env.example`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (app/config.py) с полями ниже; синглтон `settings`; `setup_logging()` (app/logging.py).

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "sysadmin-agents"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "aiogram>=3.4",
    "openai>=1.12",
    "aiodocker>=0.21",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "structlog>=24.1",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["app*"]
```

- [ ] **Step 2: Write `.env.example`**

```dotenv
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
TELEGRAM_BOT_TOKEN=123:abc
TELEGRAM_USER_ID=123456789
COMPOSE_PROJECTS_DIR=/opt
AGENT_MAX_ITERATIONS=10
CONFIRMATION_TIMEOUT_SECONDS=300
AUDIT_LOG_PATH=/data/audit.log
```

- [ ] **Step 3: Write `app/config.py`**

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_api_key: str = Field(alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_user_id: int = Field(alias="TELEGRAM_USER_ID")

    compose_projects_dir: str = Field(default="/opt", alias="COMPOSE_PROJECTS_DIR")
    agent_max_iterations: int = Field(default=10, alias="AGENT_MAX_ITERATIONS")
    confirmation_timeout_seconds: int = Field(
        default=300, alias="CONFIRMATION_TIMEOUT_SECONDS"
    )
    audit_log_path: str = Field(default="/data/audit.log", alias="AUDIT_LOG_PATH")


settings = Settings()
```

- [ ] **Step 4: Write `app/logging.py`**

```python
import logging
import structlog
from app.config import settings


def setup_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        handlers=[logging.StreamHandler()],
    )
    file_handler = logging.FileHandler(settings.audit_log_path)
    file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str):
    return structlog.get_logger(name)
```

- [ ] **Step 5: Write failing test `tests/test_config.py`**

```python
import os
from app.config import Settings


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "1")
    s = Settings()
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.llm_model == "deepseek-chat"
    assert s.agent_max_iterations == 10
    assert s.compose_projects_dir == "/opt"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: Write `tests/conftest.py`** (shared fixtures stub)

```python
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .env.example app/ tests/
git commit -m "chore: project scaffolding + config"
```

---

## Task 2: LLM Client

**Files:**
- Create: `app/llm/__init__.py`, `app/llm/client.py`
- Create: `tests/test_llm_client.py`

**Interfaces:**
- Produces: `LLMClient(api_key, base_url, model)` with `async def chat(messages: list[dict], tools: list[dict] | None = None) -> ChoiceMessage`, where `ChoiceMessage` has `.content: str | None` and `.tool_calls: list[ToolCall] | None`, `ToolCall.id`, `ToolCall.function.name`, `ToolCall.function.arguments (str)`.

- [ ] **Step 1: Write failing test `tests/test_llm_client.py`**

```python
import json
from unittest.mock import AsyncMock, patch
from app.llm.client import LLMClient


async def test_chat_returns_text():
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    fake_msg = type("M", (), {"content": "hi", "tool_calls": None})()
    with patch.object(client._client.chat.completions, "create", new=AsyncMock(return_value=type("R", (), {"choices": [type("C", (), {"message": fake_msg})()]})())):
        msg = await client.chat([{"role": "user", "content": "hello"}])
        assert msg.content == "hi"
        assert msg.tool_calls is None


async def test_chat_returns_tool_calls():
    client = LLMClient(api_key="k", base_url="http://x", model="m")
    tc = type("F", (), {"name": "docker_ps", "arguments": json.dumps({})})()
    fake_msg = type("M", (), {"content": None, "tool_calls": [type("T", (), {"id": "c1", "function": tc})()]})()
    with patch.object(client._client.chat.completions, "create", new=AsyncMock(return_value=type("R", (), {"choices": [type("C", (), {"message": fake_msg})()]})())):
        msg = await client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "docker_ps"}}])
        assert msg.tool_calls[0].function.name == "docker_ps"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_client.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/llm/client.py`**

```python
from dataclasses import dataclass
from typing import Any
from openai import AsyncOpenAI, NOT_GIVEN


@dataclass
class ToolCallFunction:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: ToolCallFunction


@dataclass
class ChoiceMessage:
    content: str | None
    tool_calls: list[ToolCall] | None


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ChoiceMessage:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools if tools else NOT_GIVEN,
        )
        m = resp.choices[0].message
        tool_calls = None
        if m.tool_calls:
            tool_calls = [
                ToolCall(id=tc.id, function=ToolCallFunction(
                    name=tc.function.name, arguments=tc.function.arguments))
                for tc in m.tool_calls
            ]
        return ChoiceMessage(content=m.content, tool_calls=tool_calls)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/llm/ tests/test_llm_client.py
git commit -m "feat: generic LLM client"
```

---

## Task 3: Tools Base

**Files:**
- Create: `app/tools/__init__.py`, `app/tools/base.py`
- Create: `tests/test_tools_base.py`

**Interfaces:**
- Produces: `Safety` enum (`SAFE`, `DANGEROUS`); `Tool` dataclass with `name, description, params_model: type[BaseModel], fn, safety`, methods `schema() -> dict` (OpenAI function schema) and `async execute(raw_args: dict) -> str` (validates via pydantic, returns JSON string, validation errors returned as JSON `{"error": ...}`).

- [ ] **Step 1: Write failing test `tests/test_tools_base.py`**

```python
import json
from pydantic import BaseModel
from app.tools.base import Tool, Safety


class EchoParams(BaseModel):
    text: str


async def _echo(text: str) -> dict:
    return {"echo": text}


async def test_schema_format():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    s = t.schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "echo"
    assert s["function"]["parameters"]["properties"]["text"]["type"] == "string"


async def test_execute_valid():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    out = await t.execute({"text": "hi"})
    assert json.loads(out) == {"echo": "hi"}


async def test_execute_invalid_returns_error():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    out = json.loads(await t.execute({}))
    assert "error" in out


async def test_default_safety_is_safe():
    t = Tool(name="echo", description="d", params_model=EchoParams, fn=_echo)
    assert t.safety is Safety.SAFE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tools_base.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/tools/base.py`**

```python
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from pydantic import BaseModel, ValidationError


class Safety(str, Enum):
    SAFE = "safe"
    DANGEROUS = "dangerous"


@dataclass
class Tool:
    name: str
    description: str
    params_model: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]
    safety: Safety = Safety.SAFE

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_model.model_json_schema(),
            },
        }

    async def execute(self, raw_args: dict) -> str:
        try:
            params = self.params_model.model_validate(raw_args or {})
        except ValidationError as e:
            return json.dumps({"error": e.errors(include_url=False)})
        result = await self.fn(**params.model_dump())
        return _to_json(result)


def _to_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        return str(value)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tools_base.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/tools/base.py tests/test_tools_base.py
git commit -m "feat: tool abstraction with safety + schema"
```

---

## Task 4: Docker Tools

**Files:**
- Create: `app/tools/docker.py`
- Create: `tests/test_tools_docker.py`

**Interfaces:**
- Consumes: `Tool`, `Safety` from tools/base; `settings.compose_projects_dir`.
- Produces: async functions `docker_ps, docker_logs, docker_stats, docker_inspect, compose_ls, compose_ps` (SAFE) и `docker_restart, docker_stop, docker_start, compose_up, compose_down, docker_exec` (DANGEROUS); функцию `build_sysadmin_tools() -> list[Tool]`, возвращающую все обёрнутые `Tool`.

- [ ] **Step 1: Write failing test `tests/test_tools_docker.py`** (mock aiodocker + subprocess)

```python
import json
from unittest.mock import AsyncMock, patch, MagicMock
from app.tools import docker as dk
from app.tools.base import Safety


async def test_docker_ps_safe():
    fake = AsyncMock()
    fake.containers.list = AsyncMock(return_value=[
        MagicMock(attrs={"Id": "abc", "Names": ["bot"], "State": "running", "Image": "img", "Status": "Up"}),
    ])
    with patch("app.tools.docker.Docker", return_value=fake):
        out = json.loads(await dk.docker_ps())
    assert out["containers"][0]["Names"] == ["bot"]
    assert out["containers"][0]["State"] == "running"


async def test_docker_logs():
    fake = AsyncMock()
    c = MagicMock()
    c.log = AsyncMock(return_value=["line1\n", "line2\n"])
    fake.containers.container = MagicMock(return_value=c)
    with patch("app.tools.docker.Docker", return_value=fake):
        out = json.loads(await dk.docker_logs(container="bot", tail=2))
    assert out["logs"] == "line1\nline2\n"


async def test_docker_restart_dangerous():
    fake = AsyncMock()
    c = MagicMock(); c.restart = AsyncMock(return_value=None)
    fake.containers.container = MagicMock(return_value=c)
    with patch("app.tools.docker.Docker", return_value=fake):
        out = json.loads(await dk.docker_restart(container="bot"))
    assert out["restarted"] == "bot"


async def test_build_sysadmin_tools_classifications():
    tools = dk.build_sysadmin_tools()
    by_name = {t.name: t for t in tools}
    assert by_name["docker_ps"].safety is Safety.SAFE
    assert by_name["docker_logs"].safety is Safety.SAFE
    assert by_name["docker_restart"].safety is Safety.DANGEROUS
    assert by_name["compose_up"].safety is Safety.DANGEROUS
    assert by_name["docker_exec"].safety is Safety.DANGEROUS
    assert "shell_exec" not in by_name


async def test_compose_ls(tmp_path, monkeypatch):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "docker-compose.yml").write_text("x: 1")
    monkeypatch.setattr(dk.settings, "compose_projects_dir", str(tmp_path))
    out = json.loads(await dk.compose_ls())
    assert out["projects"] == ["web"]


async def test_compose_up_runs_subprocess(monkeypatch):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    with patch("app.tools.docker.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as m:
        out = json.loads(await dk.compose_up(project="web"))
    assert out["returncode"] == 0
    args = m.call_args.args
    assert "up" in args and "-d" in args
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tools_docker.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/tools/docker.py`**

```python
import asyncio
import os
from pydantic import BaseModel, Field
from aiodocker import Docker
from app.tools.base import Tool, Safety
from app.config import settings


# ---------- params ----------

class ContainerParams(BaseModel):
    container: str = Field(description="container name or id")


class LogsParams(BaseModel):
    container: str
    tail: int = Field(default=200, description="number of trailing log lines")


class ExecParams(BaseModel):
    container: str
    command: list[str] = Field(description="command argv to run inside container")


class ProjectParams(BaseModel):
    project: str = Field(description="compose project dir name under COMPOSE_PROJECTS_DIR")


# ---------- safe docker api ----------

async def docker_ps() -> dict:
    async with Docker() as docker:
        containers = await docker.containers.list(all=True)
        return {"containers": [c["attrs"] for c in containers]}


async def docker_logs(container: str, tail: int = 200) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        logs = await c.log(stdout=True, stderr=True, tail=tail)
        return {"container": container, "logs": "".join(logs)}


async def docker_stats(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        stats = await c.stats(stream=False)
        return {"container": container, "stats": stats}


async def docker_inspect(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        info = await c.show()
        return {"container": container, "inspect": info}


# ---------- dangerous docker api ----------

async def docker_restart(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        await c.restart()
        return {"restarted": container}


async def docker_stop(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        await c.stop()
        return {"stopped": container}


async def docker_start(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        await c.start()
        return {"started": container}


async def docker_exec(container: str, command: list[str]) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        exec_obj = await c.exec(cmd=command)
        stream = exec_obj.start(detach=False)
        output = ""
        async for chunk in stream:
            if isinstance(chunk, bytes):
                output += chunk.decode(errors="replace")
            else:
                output += str(chunk)
        inspect = await exec_obj.inspect()
        return {"container": container, "command": command, "output": output, "exit_code": inspect.get("ExitCode")}


# ---------- compose (subprocess) ----------

def _project_dir(project: str) -> str:
    return os.path.join(settings.compose_projects_dir, project)


async def _compose(project: str, *args: str) -> dict:
    pd = _project_dir(project)
    cmd = ["docker", "compose", "--project-directory", pd, "-f", os.path.join(pd, "docker-compose.yml"), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "project": project,
        "command": list(args),
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


async def compose_ls() -> dict:
    root = settings.compose_projects_dir
    projects = []
    for name in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, name, "docker-compose.yml")):
            projects.append(name)
    return {"projects": projects}


async def compose_ps(project: str) -> dict:
    return await _compose(project, "ps", "--format", "json")


async def compose_up(project: str) -> dict:
    return await _compose(project, "up", "-d")


async def compose_down(project: str) -> dict:
    return await _compose(project, "down")


# ---------- registry ----------

def build_sysadmin_tools() -> list[Tool]:
    return [
        Tool("docker_ps", "List all docker containers", ContainerParams, docker_ps, Safety.SAFE),
        Tool("docker_logs", "Get container logs", LogsParams, docker_logs, Safety.SAFE),
        Tool("docker_stats", "Get container resource stats", ContainerParams, docker_stats, Safety.SAFE),
        Tool("docker_inspect", "Inspect container details", ContainerParams, docker_inspect, Safety.SAFE),
        Tool("compose_ls", "List docker compose projects under COMPOSE_PROJECTS_DIR", BaseModel, compose_ls, Safety.SAFE),
        Tool("compose_ps", "List services of a compose project", ProjectParams, compose_ps, Safety.SAFE),
        Tool("docker_restart", "Restart a container (DESTRUCTIVE)", ContainerParams, docker_restart, Safety.DANGEROUS),
        Tool("docker_stop", "Stop a container (DESTRUCTIVE)", ContainerParams, docker_stop, Safety.DANGEROUS),
        Tool("docker_start", "Start a container (DESTRUCTIVE)", ContainerParams, docker_start, Safety.DANGEROUS),
        Tool("compose_up", "Run docker compose up -d for a project (DESTRUCTIVE)", ProjectParams, compose_up, Safety.DANGEROUS),
        Tool("compose_down", "Run docker compose down for a project (DESTRUCTIVE)", ProjectParams, compose_down, Safety.DANGEROUS),
        Tool("docker_exec", "Run a command inside a container (DESTRUCTIVE)", ExecParams, docker_exec, Safety.DANGEROUS),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tools_docker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/tools/docker.py tests/test_tools_docker.py
git commit -m "feat: docker & compose tools with safety classification"
```

---

## Task 5: Agent Messages & Registry & Confirmation Gateway

**Files:**
- Create: `app/agents/__init__.py`, `app/agents/messages.py`, `app/agents/registry.py`
- Create: `tests/test_messages.py`, `tests/test_registry.py`

**Interfaces:**
- Produces (messages.py): `Task(id: str, content: str)`, `Result(task_id: str, content: str, success: bool = True)`, `ConfirmationRequest(task_id: str, tool_name: str, args: dict, description: str)`.
- Produces (registry.py): `ConfirmationGateway` Protocol with `async request(req: ConfirmationRequest) -> bool`; `AgentRegistry` with `register(agent)`, `async request(agent_name: str, task: Task) -> Result`, `async respond(result: Result)`, `async confirm(req) -> bool`, `set_confirmation_gateway(gw)`, `async run_forever()` (запускает consumers всех агентов), `async stop()`, and `get_agent(name)`, `available_agents()`.

- [ ] **Step 1: Write failing test `tests/test_messages.py`**

```python
from app.agents.messages import Task, Result, ConfirmationRequest


def test_task_has_id():
    t1 = Task(content="x")
    t2 = Task(content="y")
    assert t1.id != t2.id
    assert t1.content == "x"


def test_result_defaults():
    r = Result(task_id="1", content="ok")
    assert r.success is True
```

- [ ] **Step 2: Write `app/agents/messages.py`**

```python
import uuid
from pydantic import BaseModel, Field


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str


class Result(BaseModel):
    task_id: str
    content: str
    success: bool = True


class ConfirmationRequest(BaseModel):
    task_id: str
    tool_name: str
    args: dict
    description: str
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/test_messages.py -v`
Expected: PASS

- [ ] **Step 4: Write failing test `tests/test_registry.py`** (two fake agents talking)

```python
import asyncio
import pytest
from app.agents.messages import Task, Result, ConfirmationRequest
from app.agents.registry import AgentRegistry, ConfirmationGateway


class FakeAgent:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def handle(self, task: Task) -> Result:
        return await self._handler(task)


class FakeGateway:
    def __init__(self, decision: bool):
        self.decision = decision
        self.received: list[ConfirmationRequest] = []

    async def request(self, req: ConfirmationRequest) -> bool:
        self.received.append(req)
        return self.decision


async def test_delegate_returns_result():
    reg = AgentRegistry()
    reg.register(FakeAgent("sysadmin", lambda t: Result(task_id=t.id, content="done")))
    reg.set_confirmation_gateway(FakeGateway(True))
    asyncio.create_task(reg._consume("sysadmin"))
    res = await reg.request("sysadmin", Task(content="hi"))
    await reg.stop()
    assert res.content == "done"


async def test_confirm_routes_to_gateway():
    reg = AgentRegistry()
    gw = FakeGateway(False)
    reg.set_confirmation_gateway(gw)
    decision = await reg.confirm(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={}, description="d"))
    assert decision is False
    assert gw.received[0].tool_name == "docker_restart"
```

- [ ] **Step 5: Run test to verify it fails**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL (module not found)

- [ ] **Step 6: Write `app/agents/registry.py`**

```python
import asyncio
from typing import Protocol
from app.agents.messages import Task, Result, ConfirmationRequest
from app.logging import get_logger

log = get_logger("registry")


class ConfirmationGateway(Protocol):
    async def request(self, req: ConfirmationRequest) -> bool: ...


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, object] = {}
        self._inboxes: dict[str, asyncio.Queue[Task]] = {}
        self._pending: dict[str, asyncio.Future[Result]] = {}
        self._gateway: ConfirmationGateway | None = None
        self._tasks: list[asyncio.Task] = []

    def register(self, agent) -> None:
        self._agents[agent.name] = agent
        self._inboxes[agent.name] = asyncio.Queue()

    def set_confirmation_gateway(self, gw: ConfirmationGateway) -> None:
        self._gateway = gw

    def get_agent(self, name: str):
        return self._agents[name]

    def available_agents(self) -> list[str]:
        return list(self._agents.keys())

    async def request(self, agent_name: str, task: Task) -> Result:
        fut = asyncio.get_event_loop().create_future()
        self._pending[task.id] = fut
        await self._inboxes[agent_name].put(task)
        return await fut

    async def respond(self, result: Result) -> None:
        fut = self._pending.pop(result.task_id, None)
        if fut and not fut.done():
            fut.set_result(result)

    async def confirm(self, req: ConfirmationRequest) -> bool:
        if self._gateway is None:
            return False
        return await self._gateway.request(req)

    async def _consume(self, name: str) -> None:
        agent = self._agents[name]
        inbox = self._inboxes[name]
        while True:
            task = await inbox.get()
            try:
                result = await agent.handle(task)
            except Exception as e:
                log.error("agent_failed", agent=name, error=str(e))
                result = Result(task_id=task.id, content=f"error: {e}", success=False)
            await self.respond(result)

    async def run_forever(self) -> None:
        for name in self._agents:
            self._tasks.append(asyncio.create_task(self._consume(name)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_registry.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/agents/messages.py app/agents/registry.py tests/test_messages.py tests/test_registry.py
git commit -m "feat: agent messages, registry bus, confirmation gateway protocol"
```

---

## Task 6: Agent Base Runtime (LLM↔tools loop)

**Files:**
- Create: `app/agents/base.py`
- Create: `tests/test_agent_base.py`

**Interfaces:**
- Consumes: `LLMClient`, `Tool`, `Safety`, `AgentRegistry`, `Task`, `Result`, `ConfirmationRequest`, `settings.agent_max_iterations`.
- Produces: `Agent` base class with `name: str`, `system_prompt: str`, `tools: list[Tool]`, constructed with `(llm: LLMClient, registry: AgentRegistry)`; method `async handle(task: Task) -> Result` implementing the loop; helper `_find_tool(name)`; dangerous tools trigger `await registry.confirm(...)` → if False, tool result is `{"error": "rejected by user"}`.

- [ ] **Step 1: Write failing test `tests/test_agent_base.py`** (mock LLM returning tool call then final text)

```python
import json
from unittest.mock import AsyncMock
from pydantic import BaseModel
from app.agents.base import Agent
from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result, ConfirmationRequest
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

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


async def test_agent_runs_safe_tool_then_answers():
    tool = make_tool()
    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="echo", arguments=json.dumps({"x": "hi"})))])
    final = ChoiceMessage(content="result: hi", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    agent = Agent(name="t", system_prompt="sys", tools=[tool], llm=llm, registry=reg)
    res = await agent.handle(Task(content="do it"))
    assert res.success is True
    assert "result" in res.content


async def test_dangerous_rejected():
    class Q(BaseModel):
        c: str

    async def _danger(c: str) -> dict:
        return {"done": c}

    dt = Tool(name="restart", description="d", params_model=Q, fn=_danger, safety=Safety.DANGEROUS)

    class NoGateway:
        async def request(self, req: ConfirmationRequest) -> bool:
            return False

    tc = ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="restart", arguments=json.dumps({"c": "bot"})))])
    final = ChoiceMessage(content="rejected", tool_calls=None)
    llm = FakeLLM([tc, final])
    reg = AgentRegistry()
    reg.set_confirmation_gateway(NoGateway())
    agent = Agent(name="t", system_prompt="sys", tools=[dt], llm=llm, registry=reg)
    res = await agent.handle(Task(content="restart bot"))
    assert res.success is True
    assert "rejected" in res.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent_base.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/agents/base.py`**

```python
import json
from app.config import settings
from app.llm.client import LLMClient
from app.tools.base import Tool, Safety
from app.agents.messages import Task, Result, ConfirmationRequest
from app.agents.registry import AgentRegistry
from app.logging import get_logger

log = get_logger("agent")


class Agent:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools: list[Tool],
        llm: LLMClient,
        registry: AgentRegistry,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self._llm = llm
        self._registry = registry

    def _find_tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    async def handle(self, task: Task) -> Result:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task.content},
        ]
        for _ in range(settings.agent_max_iterations):
            msg = await self._llm.chat(messages, [t.schema() for t in self.tools])
            if not msg.tool_calls:
                return Result(task_id=task.id, content=msg.content or "")
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                tool = self._find_tool(tc.function.name)
                if tool is None:
                    out = json.dumps({"error": f"unknown tool {tc.function.name}"})
                else:
                    args = json.loads(tc.function.arguments or "{}")
                    if tool.safety is Safety.DANGEROUS:
                        req = ConfirmationRequest(
                            task_id=task.id,
                            tool_name=tool.name,
                            args=args,
                            description=f"{tool.name} {args}",
                        )
                        log.info("confirmation_required", agent=self.name, tool=tool.name, args=args)
                        ok = await self._registry.confirm(req)
                        if not ok:
                            out = json.dumps({"error": "rejected by user"})
                        else:
                            out = await tool.execute(args)
                    else:
                        out = await tool.execute(args)
                log.info("tool_call", agent=self.name, tool=tc.function.name, result_preview=out[:200])
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        return Result(task_id=task.id, content="max iterations reached", success=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent_base.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/agents/base.py tests/test_agent_base.py
git commit -m "feat: agent runtime loop with confirmation interception"
```

---

## Task 7: Director Agent + delegate_to tool

**Files:**
- Create: `app/agents/director.py`
- Create: `tests/test_director.py`

**Interfaces:**
- Consumes: `Agent` (base), `Tool`, `AgentRegistry`.
- Produces: `Director(llm, registry, available_agents: dict[str,str])` subclass; `DelegateToParams(agent_name: str, task: str)`; tool `delegate_to` (SAFE) whose fn calls `await registry.request(agent_name, Task(content=task))` and returns `{"agent":..., "result":..., "success":...}`; `build_director_prompt(available_agents) -> str` system prompt listing agents.

- [ ] **Step 1: Write failing test `tests/test_director.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_director.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/agents/director.py`**

```python
from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.messages import Task, Result
from app.agents.registry import AgentRegistry
from app.llm.client import LLMClient
from app.tools.base import Tool, Safety


class DelegateToParams(BaseModel):
    agent_name: str = Field(description="name of the specialist agent to delegate to")
    task: str = Field(description="clear, self-contained task description for the specialist")


def build_director_prompt(available_agents: dict[str, str]) -> str:
    agents = "\n".join(f"- {name}: {desc}" for name, desc in available_agents.items())
    return (
        "Ты — Директор команды системных администраторов. Получаешь задачи от пользователя "
        "через Telegram. Твоя роль: понять задачу, при необходимости разбить её и делегировать "
        "подходящему специалисту через инструмент delegate_to. У тебя НЕТ прямого доступа к Docker. "
        "Получив результат от специалиста, сформулируй понятный итоговый отчёт для пользователя на русском. "
        "Если задача тривиальная и не требует специалиста — ответь сразу.\n\n"
        f"Доступные специалисты:\n{agents}"
    )


class Director(Agent):
    def __init__(self, llm: LLMClient, registry: AgentRegistry, available_agents: dict[str, str]):
        async def _delegate(agent_name: str, task: str) -> dict:
            result = await registry.request(agent_name, Task(content=task))
            return {"agent": agent_name, "result": result.content, "success": result.success}

        delegate_tool = Tool(
            name="delegate_to",
            description="Delegate a task to a specialist agent and receive its result",
            params_model=DelegateToParams,
            fn=_delegate,
            safety=Safety.SAFE,
        )
        super().__init__(
            name="director",
            system_prompt=build_director_prompt(available_agents),
            tools=[delegate_tool],
            llm=llm,
            registry=registry,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_director.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/agents/director.py tests/test_director.py
git commit -m "feat: director agent with delegate_to tool"
```

---

## Task 8: Chief Sysadmin Agent

**Files:**
- Create: `app/agents/sysadmin.py`
- Create: `tests/test_sysadmin.py`

**Interfaces:**
- Consumes: `Agent` (base), `build_sysadmin_tools()` (tools/docker.py).
- Produces: `ChiefSysadmin(llm, registry)` subclass with docker tools and a Russian system prompt. Must call `build_sysadmin_tools()` at construction so monkeypatched functions are picked up.

- [ ] **Step 1: Write failing test `tests/test_sysadmin.py`**

```python
import json
from app.agents.sysadmin import ChiefSysadmin
from app.agents.registry import AgentRegistry
from app.agents.messages import Task
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.tools import docker as dk


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


async def test_sysadmin_has_docker_tools(monkeypatch):
    reg = AgentRegistry()
    agent = ChiefSysadmin(llm=FakeLLM([]), registry=reg)
    names = {t.name for t in agent.tools}
    assert {"docker_ps", "docker_logs", "docker_restart"} <= names
    assert "shell_exec" not in names


async def test_sysadmin_calls_docker_ps(monkeypatch):
    async def fake_ps():
        return {"containers": [{"Names": ["bot"], "State": "running"}]}
    monkeypatch.setattr(dk, "docker_ps", fake_ps)
    reg = AgentRegistry()
    agent = ChiefSysadmin(llm=FakeLLM([
        ChoiceMessage(content=None, tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="docker_ps", arguments=json.dumps({})))]),
        ChoiceMessage(content="Найден контейнер bot, работает.", tool_calls=None),
    ]), registry=reg)
    res = await agent.handle(Task(content="покажи контейнеры"))
    assert "bot" in res.content
```

Note: `ChiefSysadmin.__init__` calls `build_sysadmin_tools()` at construction time. The monkeypatch on `dk.docker_ps` must happen BEFORE construction for the patched fn to be wired into the Tool. If the test wires the patch after construction, rebuild tools. The test above patches before constructing `ChiefSysadmin`, so it works.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sysadmin.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/agents/sysadmin.py`**

```python
from app.agents.base import Agent
from app.agents.registry import AgentRegistry
from app.llm.client import LLMClient
from app.tools.docker import build_sysadmin_tools

SYSADMIN_PROMPT = (
    "Ты — Главный системный администратор. Ты управляешь Docker-контейнерами и compose-проектами "
    "на текущем сервере. Получаешь технические подзадачи от Директора. Используй инструменты для "
    "сбора данных (логи, статусы, инспекция) и анализа. Опасные операции (перезапуск, остановка, "
    "exec, compose up/down) требуют подтверждения пользователя — система спросит его автоматически, "
    "просто вызывай нужный инструмент. Возвращай структурированный, технически точный отчёт на русском. "
    "Не выдумывай данные — только то, что вернули инструменты."
)


class ChiefSysadmin(Agent):
    def __init__(self, llm: LLMClient, registry: AgentRegistry):
        super().__init__(
            name="sysadmin",
            system_prompt=SYSADMIN_PROMPT,
            tools=build_sysadmin_tools(),
            llm=llm,
            registry=registry,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sysadmin.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/agents/sysadmin.py tests/test_sysadmin.py
git commit -m "feat: chief sysadmin agent with docker tools"
```

---

## Task 9: Telegram Bot (filters, keyboards, gateway, handlers, assembly)

**Files:**
- Create: `app/bot/__init__.py`, `app/bot/filters.py`, `app/bot/keyboards.py`, `app/bot/gateway.py`, `app/bot/handlers.py`, `app/bot/bot.py`
- Create: `tests/test_bot_filters.py`, `tests/test_bot_gateway.py`, `tests/test_bot_handlers.py`

**Interfaces:**
- Consumes: `Settings` (`telegram_user_id`, `telegram_bot_token`, `confirmation_timeout_seconds`), `AgentRegistry`, `ConfirmationRequest`, `Director`.
- Produces: `WhitelistFilter(allowed_id)`; `approve_keyboard(task_id, tool_name) -> InlineKeyboardMarkup`; `TelegramConfirmationGateway(bot, chat_id, timeout=None)` impl `ConfirmationGateway` with `_resolve(task_id, decision)`; `build_router(*, registry, allowed_id, chat_id) -> Router`; `create_bot() -> Bot`; `create_dispatcher(*, registry) -> Dispatcher`.

- [ ] **Step 1: Write `tests/test_bot_filters.py`**

```python
from unittest.mock import MagicMock
from app.bot.filters import WhitelistFilter


def _user_msg(uid):
    m = MagicMock()
    m.from_user = MagicMock(id=uid)
    return m


async def test_whitelist_allows_known_user():
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(42)) is True


async def test_whitelist_blocks_unknown_user():
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(999)) is False
```

- [ ] **Step 2: Write `app/bot/filters.py`**

```python
from aiogram.filters import BaseFilter
from aiogram.types import Message


class WhitelistFilter(BaseFilter):
    def __init__(self, allowed_id: int):
        self._allowed = allowed_id

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id == self._allowed
```

- [ ] **Step 3: Run filter test to verify it passes**

Run: `pytest tests/test_bot_filters.py -v`
Expected: PASS

- [ ] **Step 4: Write `tests/test_bot_gateway.py`**

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.bot.gateway import TelegramConfirmationGateway
from app.agents.messages import ConfirmationRequest


async def test_gateway_returns_true_on_approve():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=123, timeout=5)
    asyncio.get_event_loop().call_later(0.05, lambda: gw._resolve("x", True))
    res = await gw.request(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={"container": "bot"}, description="restart bot"))
    assert res is True


async def test_gateway_returns_false_on_timeout():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=123, timeout=0)
    res = await gw.request(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={}, description="d"))
    assert res is False
```

- [ ] **Step 5: Write `app/bot/keyboards.py`**

```python
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def approve_keyboard(task_id: str, tool_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Approve", callback_data=f"cf:{task_id}:yes"),
            InlineKeyboardButton(text="Reject", callback_data=f"cf:{task_id}:no"),
        ]
    ])
```

- [ ] **Step 6: Write `app/bot/gateway.py`**

```python
import asyncio
from aiogram import Bot
from app.agents.registry import ConfirmationGateway
from app.agents.messages import ConfirmationRequest
from app.bot.keyboards import approve_keyboard
from app.config import settings


class TelegramConfirmationGateway(ConfirmationGateway):
    def __init__(self, bot: Bot, chat_id: int, timeout: int | None = None):
        self._bot = bot
        self._chat_id = chat_id
        self._timeout = timeout if timeout is not None else settings.confirmation_timeout_seconds
        self._pending: dict[str, asyncio.Future[bool]] = {}

    def _resolve(self, task_id: str, decision: bool) -> None:
        fut = self._pending.get(task_id)
        if fut and not fut.done():
            fut.set_result(decision)

    async def request(self, req: ConfirmationRequest) -> bool:
        fut = asyncio.get_event_loop().create_future()
        self._pending[req.task_id] = fut
        text = f"Подтвердите опасное действие:\n{req.tool_name} {req.args}\n\n{req.description}"
        await self._bot.send_message(
            self._chat_id, text, reply_markup=approve_keyboard(req.task_id, req.tool_name)
        )
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(req.task_id, None)
```

- [ ] **Step 7: Run gateway test to verify it passes**

Run: `pytest tests/test_bot_gateway.py -v`
Expected: PASS

- [ ] **Step 8: Write `tests/test_bot_handlers.py`**

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.bot.handlers import build_router
from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result
from app.bot.gateway import TelegramConfirmationGateway


class FakeDirector:
    name = "director"
    def __init__(self, result_text):
        self._result_text = result_text
    async def handle(self, task: Task) -> Result:
        return Result(task_id=task.id, content=self._result_text)


async def test_callback_approve_resolves_gateway():
    reg = AgentRegistry()
    bot = MagicMock(); bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=1, timeout=30)
    reg.set_confirmation_gateway(gw)
    router = build_router(registry=reg, allowed_id=42, chat_id=42)

    fut = asyncio.get_event_loop().create_future()
    gw._pending["abc"] = fut

    cq = MagicMock()
    cq.from_user = MagicMock(id=42)
    cq.data = "cf:abc:yes"
    cq.answer = AsyncMock()
    cq.message = MagicMock(); cq.message.edit_text = AsyncMock()

    for cb in router.callback_query.handlers:
        await cb.callback(cq, registry=reg)
    assert fut.result() is True
```

Note: aiogram handler introspection (`router.message.handlers` / `router.callback_query.handlers`) is used in tests. If the exact attribute name differs across aiogram versions, replace with `dp.feed_update(bot, update)` using a synthetic `Update`. The message→Director path is covered indirectly by Task 5+7 integration; here we directly assert the callback resolution path.

- [ ] **Step 9: Write `app/bot/handlers.py`**

```python
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from app.agents.messages import Task
from app.agents.registry import AgentRegistry
from app.bot.filters import WhitelistFilter


def build_router(*, registry: AgentRegistry, allowed_id: int, chat_id: int) -> Router:
    router = Router()

    @router.message(WhitelistFilter(allowed_id), Command("start"))
    async def _start(message: Message):
        await message.answer("Система активна. Опишите задачу.")

    @router.message(WhitelistFilter(allowed_id))
    async def _task(message: Message, registry: AgentRegistry):
        task = Task(content=message.text or "")
        result = await registry.request("director", task)
        await message.answer(result.content)

    @router.callback_query(F.data.startswith("cf:"))
    async def _confirm(callback: CallbackQuery, registry: AgentRegistry):
        _, task_id, decision = callback.data.split(":")
        from app.bot.gateway import TelegramConfirmationGateway
        gw = registry._gateway
        if isinstance(gw, TelegramConfirmationGateway):
            gw._resolve(task_id, decision == "yes")
        await callback.answer("Подтверждено" if decision == "yes" else "Отклонено")
        await callback.message.edit_text(
            callback.message.text + f"\n\n> Решение: {'Approve' if decision == 'yes' else 'Reject'}"
        )

    return router
```

- [ ] **Step 10: Run handler tests to verify they pass**

Run: `pytest tests/test_bot_handlers.py -v`
Expected: PASS

- [ ] **Step 11: Write `app/bot/bot.py`**

```python
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from app.bot.handlers import build_router
from app.agents.registry import AgentRegistry
from app.config import settings


def create_bot() -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


def create_dispatcher(*, registry: AgentRegistry) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(
        registry=registry,
        allowed_id=settings.telegram_user_id,
        chat_id=settings.telegram_user_id,
    ))
    return dp
```

- [ ] **Step 12: Commit**

```bash
git add app/bot/ tests/test_bot_*.py
git commit -m "feat: telegram bot - whitelist, confirmation gateway, handlers"
```

---

## Task 10: main.py Wiring & Lifecycle

**Files:**
- Create: `app/main.py`

**Interfaces:**
- Consumes: all components. Produces: runnable `async def main()` that sets up logging, LLMClient, registry, registers Director + ChiefSysadmin, wires TelegramConfirmationGateway, starts registry consumers, runs aiogram polling; graceful shutdown.

- [ ] **Step 1: Write `app/main.py`**

```python
import asyncio
from app.config import settings
from app.logging import setup_logging, get_logger
from app.llm.client import LLMClient
from app.agents.registry import AgentRegistry
from app.agents.director import Director
from app.agents.sysadmin import ChiefSysadmin
from app.bot.bot import create_bot, create_dispatcher
from app.bot.gateway import TelegramConfirmationGateway

log = get_logger("main")


async def main() -> None:
    setup_logging()
    llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    registry = AgentRegistry()
    sysadmin = ChiefSysadmin(llm=llm, registry=registry)
    registry.register(sysadmin)
    director = Director(
        llm=llm, registry=registry,
        available_agents={sysadmin.name: "управление Docker-контейнерами и compose-проектами"},
    )
    registry.register(director)

    bot = create_bot()
    gateway = TelegramConfirmationGateway(bot, chat_id=settings.telegram_user_id)
    registry.set_confirmation_gateway(gateway)

    await registry.run_forever()
    dp = create_dispatcher(registry=registry)

    log.info("startup", model=settings.llm_model, agents=registry.available_agents())
    try:
        await dp.start_polling(bot)
    finally:
        await registry.stop()
        await bot.session.close()
        log.info("shutdown")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Smoke import test**

Run: `python -c "import app.main"`
Expected: no import errors (Note: `Settings()` requires env vars; for the smoke test set them or wrap in try/except. If it raises on missing env, set `LLM_API_KEY=x TELEGRAM_BOT_TOKEN=t TELEGRAM_USER_ID=1 python -c "import app.main"`.)

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "feat: wire app lifecycle and start polling"
```

---

## Task 11: Dockerfile, docker-compose.yml, AGENTS.md

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`
- Modify: `AGENTS.md` (append run/test commands)

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends docker.io docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
COPY .env.example ./

CMD ["python", "-m", "app.main"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  sysadmin-agents:
    build: .
    container_name: sysadmin-agents
    restart: unless-stopped
    env_file: .env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./audit:/data
      - ${COMPOSE_PROJECTS_DIR:-/opt}:/opt:ro
```

Note: `COMPOSE_PROJECTS_DIR` mounted read-only into `/opt` for `compose_ls` discovery. `compose_up`/`compose_down` actually run `docker compose` against files in that dir — they don't need write access to the dir itself (compose manipulates containers, not the compose file), so `ro` is fine and safer. If you later store compose files generated by the agent, switch to `rw`.

- [ ] **Step 3: Append run/test commands to `AGENTS.md`**

```markdown

## Run

- Install dev: `pip install -e ".[dev]"`
- Tests: `pytest -v`
- Local run: `python -m app.main`
- Deploy: `docker compose up -d --build`
```

- [ ] **Step 4: Verify full test suite**

Run: `pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml AGENTS.md
git commit -m "feat: docker deployment + docs"
```

---

## Self-Review

**1. Spec coverage:**
- Director + Главсисадмин → Tasks 7, 8. ✓
- Telegram управление → Task 9. ✓
- Кастомный лёгкий цикл → Task 6. ✓
- DeepSeek/generic LLM → Task 2 + config. ✓
- Чтение авто, опасные с подтверждением → Task 6 (interception) + Task 9 (gateway). ✓
- Только этот сервер, Docker на хосте → Task 4 + docker-compose socket mount. ✓
- Без БД, логи → Task 1 (logging). ✓
- Масштабируемость (реестр + делегирование) → Task 5. ✓
- Деплой через docker compose → Task 11. ✓

**2. Placeholder scan:** no TBD/TODO. Task 9 Step 1 originally had a placeholder guard; replaced with clean version.

**3. Type consistency:** `Tool.execute(raw_args)` / `Tool.schema()` едины по всем задачам. `Agent.handle(Task)->Result` — единая сигнатура в base/director/sysadmin/fakes. `ConfirmationGateway.request(ConfirmationRequest)->bool` — единый протокол в registry/gateway. `registry.request(name, task)->Result` — едино. `ChoiceMessage/ToolCall/ToolCallFunction` — из llm/client, используются в base и тестах консистентно.

**Implementation note for executor:** aiogram handler introspection in Task 9 tests may need adjustment to the installed aiogram version's internal API. If `router.callback_query.handlers` is not accessible, use `Dispatcher.feed_update()` with a synthetic `Update` object as the test harness.
