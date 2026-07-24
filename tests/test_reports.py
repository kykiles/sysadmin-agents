import asyncio
import json
from pathlib import Path

from app.agents.director import Director
from app.agents.messages import Task
from app.agents.registry import AgentRegistry
from app.bot.reports import save_report, _slug
from app.llm.client import ChoiceMessage, ToolCall, ToolCallFunction


class FakeLLM:
    def __init__(self, responses):
        self._r = responses

    async def chat(self, messages, tools=None):
        return self._r.pop(0)


def test_slug_transliterates_safely():
    assert _slug("Отчёт по нодам!") == "отчёт-по-нодам"
    assert _slug("a/b\\c") == "abc"
    assert _slug("!!!") == "report"


def test_save_report_creates_file_with_date(tmp_path):
    path = save_report(str(tmp_path), "Ноды", "| a | b |\n|---|---|")
    assert Path(path).exists()
    assert Path(path).name.startswith("ноды-")
    assert path.endswith(".md")
    assert "| a | b |" in Path(path).read_text(encoding="utf-8")


def test_save_report_adds_title_heading_when_missing(tmp_path):
    body = Path(save_report(str(tmp_path), "Ноды", "текст")).read_text(encoding="utf-8-sig")
    assert body.startswith("# Ноды\n")


def test_save_report_keeps_existing_heading(tmp_path):
    body = Path(save_report(str(tmp_path), "Ноды", "# Свой заголовок\n\nтекст")).read_text(encoding="utf-8-sig")
    assert body.startswith("# Свой заголовок")
    assert "# Ноды" not in body


def test_creates_missing_directory(tmp_path):
    path = save_report(str(tmp_path / "nested" / "reports"), "Отчёт", "текст")
    assert Path(path).exists()


async def test_director_exposes_report_path_as_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agents.director.settings", type("S", (), {"reports_dir": str(tmp_path)})())
    call = ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1",
        function=ToolCallFunction(name="make_report", arguments=json.dumps(
            {"title": "Ноды", "markdown": "# Ноды\n\n10 штук"})),
    )])
    final = ChoiceMessage(content="Отчёт готов. Нод: 10.", tool_calls=None)
    d = Director(llm=FakeLLM([call, final]), registry=AgentRegistry())
    result = await d.handle(Task(content="оформи отчёт по нодам", chat_id="c1"))

    assert result.attachment.endswith(".md")
    assert Path(result.attachment).exists()
    assert result.content == "Отчёт готов. Нод: 10."


async def test_attachment_resets_between_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agents.director.settings", type("S", (), {"reports_dir": str(tmp_path)})())
    call = ChoiceMessage(content=None, tool_calls=[ToolCall(
        id="c1",
        function=ToolCallFunction(name="make_report", arguments=json.dumps(
            {"title": "Первый", "markdown": "текст"})),
    )])
    d = Director(
        llm=FakeLLM([call, ChoiceMessage(content="готово", tool_calls=None),
                     ChoiceMessage(content="просто ответ", tool_calls=None)]),
        registry=AgentRegistry(),
    )
    first = await d.handle(Task(content="оформи отчёт", chat_id="c1"))
    second = await d.handle(Task(content="как дела", chat_id="c1"))

    assert first.attachment
    assert second.attachment == ""  # вторая задача не тащит файл первой
