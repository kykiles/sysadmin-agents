from unittest.mock import AsyncMock

import pytest

from skills.db.tools import _is_read_only, docker_query, build_tools
from app.tools.base import Safety


def test_select_allowed():
    assert _is_read_only(["psql", "-U", "u", "-d", "db", "-c", "SELECT count(*) FROM users"])
    assert _is_read_only(["mysql", "-e", "SHOW TABLES"])
    assert _is_read_only(["sqlite3", "/data/app.db", "SELECT 1"])
    assert _is_read_only(["psql", "-c", "\\dt"])


def test_writes_rejected():
    assert not _is_read_only(["psql", "-c", "DELETE FROM users"])
    assert not _is_read_only(["psql", "-c", "SELECT 1; DROP TABLE users"])
    assert not _is_read_only(["mysql", "-e", "UPDATE users SET admin=1"])
    # запись, спрятанная в CTE
    assert not _is_read_only(["psql", "-c", "WITH x AS (INSERT INTO t VALUES (1)) SELECT 1"])


def test_shell_escape_rejected():
    assert not _is_read_only(["psql", "-c", "\\! rm -rf /"])
    assert not _is_read_only(["sqlite3", "/data/app.db", ".shell sh"])
    assert not _is_read_only(["sqlite3", "/data/app.db", ".output /etc/passwd"])


def test_file_execution_rejected():
    assert not _is_read_only(["psql", "-f", "/tmp/anything.sql"])
    assert not _is_read_only(["sqlite3", "-init", "/tmp/x.sql", "/data/app.db"])


def test_non_client_binaries_rejected():
    assert not _is_read_only(["sh", "-c", "rm -rf /"])
    assert not _is_read_only(["cat", "/etc/passwd"])
    assert not _is_read_only([])


def test_interactive_shell_rejected():
    # без запроса клиент открыл бы интерактивную сессию
    assert not _is_read_only(["psql", "-U", "u", "-d", "db"])
    assert not _is_read_only(["psql", "-c"])


async def test_docker_query_rejects_write():
    out = await docker_query(container="pg", command=["psql", "-c", "DROP TABLE users"])
    assert "error" in out


async def test_docker_query_runs_via_docker_exec(monkeypatch):
    import skills.db.tools as dt

    async def fake_docker_exec(container, command):
        return {"container": container, "command": command, "output": "1", "exit_code": 0}

    monkeypatch.setattr(dt, "docker_exec", fake_docker_exec)
    out = await docker_query(container="pg", command=["psql", "-c", "SELECT 1"])
    assert out["exit_code"] == 0


def test_tool_requires_confirmation():
    """Фильтр не доказывает read-only (аудит 2026-09-12, F03) — вызов подтверждает человек."""
    (tool,) = build_tools()
    assert tool.name == "docker_query"
    assert tool.safety is Safety.DANGEROUS


def test_loaded_skill_sees_dangerous():
    from pathlib import Path
    from app.skills.loader import load_skill

    skill = load_skill(Path("skills/db"))
    assert {t.name: t.safety for t in skill.tools} == {"docker_query": Safety.DANGEROUS}


def test_psql_list_databases_allowed():
    """Без этого агент угадывал имя БД вместо того, чтобы перечислить базы."""
    assert _is_read_only(["psql", "-U", "postgres", "-l"])
    assert _is_read_only(["psql", "-U", "postgres", "--list"])


def test_listing_flag_does_not_smuggle_writes():
    assert not _is_read_only(["psql", "-l", "-c", "DROP TABLE users"])
    assert not _is_read_only(["psql", "-l", "-f", "/tmp/evil.sql"])
    assert not _is_read_only(["mysql", "-l"])


# ---------- агентный dispatch: без согласования docker_exec не вызывается ----------

# Четыре формы из аудита: фильтр их пропускает, поэтому защищает только подтверждение.
F03_FORMS = [
    ["psql", "-c", "SELECT 1", "--command=DELETE FROM audit_dummy"],
    ["psql", "-c", "\\i /tmp/audit.sql"],
    ["sqlite3", "-cmd", "DELETE FROM audit_dummy", "/tmp/test.db", "SELECT 1"],
    ["sqlite3", "/tmp/test.db", "PRAGMA user_version=42"],
]
SELECT = ["psql", "-U", "u", "-d", "db", "-c", "SELECT count(*) FROM users"]


class _LLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


class _Gateway:
    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    async def request(self, req):
        self.requests.append(req)
        return self.decision


async def _dispatch(monkeypatch, tmp_path, command, gateway) -> list:
    import json
    import skills.db.tools as dt
    from app import audit
    from app.agents.base import Agent
    from app.agents.messages import Task
    from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction

    calls = []

    async def fake_docker_exec(container, command):
        calls.append(command)
        return {"container": container, "output": "1", "exit_code": 0}

    monkeypatch.setattr(dt, "docker_exec", fake_docker_exec)
    monkeypatch.setattr(dt, "container_missing", AsyncMock(return_value=None))
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(tmp_path / "audit.jsonl"))
    args = json.dumps({"container": "pg", "command": command, "_intent": "Посмотрю данные."})
    llm = _LLM([
        ChoiceMessage(None, [ToolCall("c1", ToolCallFunction("docker_query", args))]),
        ChoiceMessage("готово", None),
    ])
    await Agent("db", "sys", build_tools(), llm, gateway=gateway).handle(Task(content="q"))
    return calls


@pytest.mark.parametrize("command", F03_FORMS + [SELECT])
async def test_no_gateway_no_exec(monkeypatch, tmp_path, command):
    assert await _dispatch(monkeypatch, tmp_path, command, gateway=None) == []


@pytest.mark.parametrize("command", F03_FORMS + [SELECT])
async def test_rejected_no_exec(monkeypatch, tmp_path, command):
    from app.agents.messages import Decision

    gw = _Gateway(Decision.REJECTED)
    assert await _dispatch(monkeypatch, tmp_path, command, gateway=gw) == []
    assert [r.tool_name for r in gw.requests] == ["docker_query"]


async def test_plain_select_asks_then_runs_once(monkeypatch, tmp_path):
    from app.agents.messages import Decision

    gw = _Gateway(Decision.APPROVED)
    assert await _dispatch(monkeypatch, tmp_path, SELECT, gateway=gw) == [SELECT]
    assert len(gw.requests) == 1
    assert gw.requests[0].args["command"] == SELECT
