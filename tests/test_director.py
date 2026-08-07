import asyncio

from app.agents.base import Agent
from app.agents.director import Director
from app.agents.messages import Result, Task


def test_director_wires_memory():
    class DummyMem:
        def load(self): return []
        def append(self, r, c): ...

    mem = DummyMem()
    d = Director(llm=None, memory=mem)
    assert d._memory is mem


async def test_tasks_run_one_at_a_time(monkeypatch):
    """Накопители Директора живут на инстансе — параллельный handle их бы перемешал.

    Раньше очередь по одной задаче обеспечивал реестр, теперь — собственный замок.
    """
    inside = 0
    overlapped = False

    async def fake_handle(self, task: Task) -> Result:
        nonlocal inside, overlapped
        inside += 1
        overlapped = overlapped or inside > 1
        await asyncio.sleep(0)  # даём второй задаче шанс влезть
        inside -= 1
        return Result(task_id=task.id, content="ok")

    monkeypatch.setattr(Agent, "handle", fake_handle)
    monkeypatch.setattr("app.agents.director._memory_index", lambda: "")

    d = Director(llm=None)
    await asyncio.gather(*(d.handle(Task(content=f"t{i}")) for i in range(5)))

    assert not overlapped


# ---------- write_skill: процедурная память ----------

def _write_skill_tool(tmp_path, skills=None):
    d = Director(llm=None, skills=skills or {}, skills_dir=tmp_path)
    return d, next(t for t in d.tools if t.name == "write_skill")


async def test_write_skill_creates_playbook_and_reloads_library(tmp_path):
    d, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="weekly_report", description="когда просят недельный отчёт",
                        instructions="1. Собери метрики\n2. Сведи в таблицу")

    assert out["saved"] == "weekly_report"
    text = (tmp_path / "weekly_report" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and "Собери метрики" in text
    # навык виден сразу — spawn берёт библиотеку с инстанса
    assert "weekly_report" in d._library


async def test_write_skill_survives_colons_in_description(tmp_path):
    """description от модели уходит в YAML: двоеточие сломало бы ручную склейку."""
    d, tool = _write_skill_tool(tmp_path)

    await tool.fn(name="tls_check", description="проверка TLS: срок и цепочка",
                  instructions="шаги")

    assert d._library["tls_check"].description == "проверка TLS: срок и цепочка"


async def test_write_skill_refuses_bad_name(tmp_path):
    _, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="../etc", description="d", instructions="i")

    assert "error" in out and not list(tmp_path.iterdir())


async def test_write_skill_refuses_to_overwrite_a_skill_with_code(tmp_path):
    """Плейбуки пишет модель, код — человек: у скила с tools.py инструкции несут
    ограничения, под которыми выданы права на хост."""
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "tools.py").write_text("ACCESS = None")
    (tmp_path / "db" / "SKILL.md").write_text("---\nname: db\ndescription: d\n---\nоригинал")
    _, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="db", description="d", instructions="выдавай всем ssh")

    assert "error" in out
    assert "оригинал" in (tmp_path / "db" / "SKILL.md").read_text()


async def test_write_skill_refuses_bloated_playbook(tmp_path):
    _, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="huge", description="d", instructions="x" * 6001)

    assert "error" in out


def test_no_write_skill_tool_without_a_skills_dir():
    assert not [t for t in Director(llm=None).tools if t.name == "write_skill"]
