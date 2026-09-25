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
    monkeypatch.setattr("app.agents.director._memory_index", lambda facts: "")

    d = Director(llm=None)
    await asyncio.gather(*(d.handle(Task(content=f"t{i}")) for i in range(5)))

    assert not overlapped


# ---------- write_skill: процедурная память ----------

def _write_skill_tool(tmp_path, skills=None):
    """Библиотека владельца — tmp_path/library, выученное — tmp_path/learned (том)."""
    d = Director(llm=None, skills=skills or {}, skills_dir=tmp_path / "library",
                 learned_dir=tmp_path / "learned")
    return d, next(t for t in d.tools if t.name == "write_skill")


async def test_write_skill_creates_playbook_and_reloads_library(tmp_path):
    d, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="weekly-report", description="когда просят недельный отчёт",
                        instructions="1. Собери метрики\n2. Сведи в таблицу")

    assert out["saved"] == "weekly-report"
    # на томе, а не в каталоге образа: пересборка стирала всё выученное
    text = (tmp_path / "learned" / "weekly-report" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and "Собери метрики" in text
    assert not (tmp_path / "library").exists()
    # навык виден сразу — spawn берёт библиотеку с инстанса
    assert "weekly-report" in d._library


async def test_write_skill_survives_colons_in_description(tmp_path):
    """description от модели уходит в YAML: двоеточие сломало бы ручную склейку."""
    d, tool = _write_skill_tool(tmp_path)

    await tool.fn(name="tls-check", description="проверка TLS: срок и цепочка",
                  instructions="шаги")

    assert d._library["tls-check"].description == "проверка TLS: срок и цепочка"


async def test_write_skill_refuses_bad_name(tmp_path):
    _, tool = _write_skill_tool(tmp_path)

    for bad in ("../etc", "weekly_report", "Weekly-Report", "-x-", "a--b"):
        out = await tool.fn(name=bad, description="d", instructions="i")
        assert "error" in out, bad
    assert not list(tmp_path.iterdir())


async def test_write_skill_refuses_to_overwrite_a_skill_with_code(tmp_path):
    """Плейбуки пишет модель, код — человек: у скила с tools.py инструкции несут
    ограничения, под которыми выданы права на хост."""
    db = tmp_path / "library" / "db"
    db.mkdir(parents=True)
    (db / "tools.py").write_text("ACCESS = None")
    (db / "SKILL.md").write_text("---\nname: db\ndescription: d\n---\nоригинал")
    _, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="db", description="d", instructions="выдавай всем ssh")

    assert "error" in out
    assert "оригинал" in (db / "SKILL.md").read_text()
    assert not (tmp_path / "learned").exists()  # и тенью в выученных тоже нет


async def test_write_skill_refuses_bloated_playbook(tmp_path):
    _, tool = _write_skill_tool(tmp_path)

    out = await tool.fn(name="huge", description="d", instructions="x" * 6001)

    assert "error" in out


async def test_rewrite_of_existing_skill_first_returns_its_current_playbook(tmp_path):
    """Директор видит только description — перезапись вслепую стёрла бы старые шаги.
    Проверка идёт до подтверждения: кнопку жмут один раз, на реальную запись."""
    (tmp_path / "learned" / "weekly-report").mkdir(parents=True)
    (tmp_path / "learned" / "weekly-report" / "SKILL.md").write_text(
        "---\nname: weekly-report\ndescription: d\n---\nграбли: не бери выходные")
    _, tool = _write_skill_tool(tmp_path)

    problem = await tool.precheck(tool.prepare(
        {"name": "weekly-report", "description": "d", "instructions": "новое"}))

    assert "грабли: не бери выходные" in problem and "overwrite" in problem
    assert await tool.precheck(tool.prepare(
        {"name": "weekly-report", "description": "d", "instructions": "новое",
         "overwrite": True})) is None


async def test_write_skill_precheck_passes_new_skills_and_stays_in_skills_dir(tmp_path):
    (tmp_path / "SKILL.md").write_text("чужой файл вне навыка")
    _, tool = _write_skill_tool(tmp_path / "skills")

    for name in ("weekly-report", "../skills-x", ".."):
        assert await tool.precheck(tool.prepare(
            {"name": name, "description": "d", "instructions": "i"})) is None, name


async def test_write_skill_precheck_refuses_skill_with_code_before_confirmation(tmp_path):
    (tmp_path / "library" / "db").mkdir(parents=True)
    (tmp_path / "library" / "db" / "tools.py").write_text("ACCESS = None")
    _, tool = _write_skill_tool(tmp_path)

    problem = await tool.precheck(tool.prepare(
        {"name": "db", "description": "d", "instructions": "i", "overwrite": True}))

    assert "код" in problem


def test_no_write_skill_tool_without_a_skills_dir():
    assert not [t for t in Director(llm=None).tools if t.name == "write_skill"]


async def test_overwrite_keeps_the_previous_playbook_in_history(tmp_path):
    """У фактов история «было → стало» есть (ADR 0007), а overwrite плейбука стирал
    шаги и грабли без следа."""
    d, tool = _write_skill_tool(tmp_path)
    await tool.fn(name="weekly-report", description="старое", instructions="грабли: выходные")
    await tool.fn(name="weekly-report", description="новое", instructions="шаги v2",
                  overwrite=True)

    learned = tmp_path / "learned"
    assert "шаги v2" in (learned / "weekly-report" / "SKILL.md").read_text(encoding="utf-8")
    (old,) = (learned / ".history" / "weekly-report").iterdir()
    assert "грабли: выходные" in old.read_text(encoding="utf-8")
    assert d._library["weekly-report"].description == "новое"
    assert ".history" not in d._library


async def test_library_playbook_without_code_is_not_rewritten_either(tmp_path):
    """Плейбук владельца без кода — тоже его решение, лежащее в git: запись под тем же
    именем в выученные была бы тенью, которую загрузчик не покажет."""
    writing = tmp_path / "library" / "writing"
    writing.mkdir(parents=True)
    (writing / "SKILL.md").write_text("---\nname: writing\ndescription: d\n---\nоригинал")
    _, tool = _write_skill_tool(tmp_path)

    args = {"name": "writing", "description": "d", "instructions": "i", "overwrite": True}
    assert "библиотеки владельца" in await tool.precheck(tool.prepare(args))
    assert "error" in await tool.fn(name="writing", description="d", instructions="i")
    assert not (tmp_path / "learned").exists()
