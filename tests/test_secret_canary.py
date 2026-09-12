"""Canary-секрет не выходит за адаптер ни в один канал (аудит 2026-09-12, F08)."""
import json
import sqlite3

import pytest
from pydantic import BaseModel

from app.agents.director import Director
from app.agents.messages import Decision, Task
from app.config import settings
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction
from app.memory import facts
from app.memory.history import DialogHistory
from app.memory.journal import TaskJournal
from app.skills.loader import Skill
from app.tools.base import Safety, Tool

CANARY = "AUDIT_FAKE_SECRET_7f3a9c"


class _NoParams(BaseModel):
    pass


class _Target(BaseModel):
    target: str


class RecordingLLM:
    def __init__(self, responses):
        self._r = list(responses)
        self.requests: list[str] = []

    async def chat(self, messages, tools=None):
        self.requests.append(json.dumps(messages, ensure_ascii=False))
        return self._r.pop(0)


def _tc(id_: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id_, ToolCallFunction(name, json.dumps(args, ensure_ascii=False)))


def _call(name: str, args: dict) -> ChoiceMessage:
    return ChoiceMessage(None, [_tc("c1", name, args)])


class _Yes:
    async def request(self, req):
        return Decision.APPROVED

    def release(self, task_id):
        pass


async def _leaky() -> dict:
    # панель вернула ключ во вложенном поле и в повторённом заголовке
    return {"nodes": [{"name": "node-1", "auth": {"token": CANARY}}],
            "stderr": f"> Authorization: Bearer {CANARY}"}


async def _mutate(target: str) -> dict:
    return {"returncode": 0, "target": target}


def _library() -> dict[str, Skill]:
    return {"panel": Skill("panel", "панель", "читай", [
        Tool("read_panel", "read", _NoParams, _leaky, Safety.SAFE),
        Tool("mutate", "change", _Target, _mutate, Safety.DANGEROUS),
    ])}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "remnawave_api_key", CANARY)
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "audit_trail_path", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(facts, "_store", facts.KnowledgeStore(str(tmp_path / "facts.db")))
    return tmp_path


def _dump(db) -> list[str]:
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return [repr(conn.execute(f"SELECT * FROM {t}").fetchall()) for t in tables]


async def test_tool_result_canary_never_reaches_llm(env):
    """Результат специалиста → его следующий запрос → ответ Директору → запрос Директора."""
    llm = RecordingLLM([
        _call("spawn", {"role": "чтец", "skills": ["panel"], "task": "прочитай ноды"}),
        _call("read_panel", {}),
        ChoiceMessage("ноды прочитаны", None),
        ChoiceMessage("Итог: ноды на месте", None),
    ])
    result = await Director(llm, skills=_library()).handle(Task(content="что с нодами?"))

    assert len(llm.requests) == 4
    assert all(CANARY not in r for r in llm.requests)
    assert "node-1" in llm.requests[2]  # полезная часть результата дошла до модели
    assert CANARY not in result.content


async def test_canary_echoed_by_model_is_not_stored(env, capsys):
    """Даже если секрет повторила модель — хранилища и ответ его не содержат."""
    history = DialogHistory(str(env / "dialog.db"), limit=20)
    journal = TaskJournal(str(env / "tasks.db"))
    llm = RecordingLLM([
        ChoiceMessage(None, [
            _tc("c1", "remember_fact", {"scope": "panel", "key": "api",
                                        "value": f"ключ {CANARY}",
                                        "description": f"когда нужен {CANARY}"}),
            _tc("c2", "make_report", {"title": f"отчёт {CANARY}",
                                      "markdown": f"# Отчёт\nключ {CANARY}"}),
        ]),
        _call("spawn", {"role": "исполнитель", "skills": ["panel"], "task": "поменяй"}),
        _call("mutate", {"target": CANARY, "_intent": "Поменяю цель."}),
        ChoiceMessage(f"сделано {CANARY}", None),
        ChoiceMessage(f"Итог {CANARY}", None),
    ])
    director = Director(llm, gateway=_Yes(), memory=history, journal=journal, skills=_library())
    result = await director.handle(Task(content=f"мой ключ {CANARY}", chat_id="1"))

    reports = list((env / "reports").iterdir())
    assert len(reports) == 1
    channels = [
        result.content, result.final,
        (env / "audit.jsonl").read_text(encoding="utf-8"),
        capsys.readouterr().out,
        *_dump(env / "dialog.db"), *_dump(env / "facts.db"), *_dump(env / "tasks.db"),
        reports[0].read_text(encoding="utf-8"),
    ]
    assert all(CANARY not in c for c in channels)
    # сами записи состоялись — секрет в них заменён, а не потерян весь факт
    assert facts._store.recall(scope="panel")[0]["value"] == "ключ <redacted>"
    assert json.loads((env / "audit.jsonl").read_text(encoding="utf-8"))["tool"] == "mutate"
