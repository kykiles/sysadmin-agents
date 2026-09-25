"""Скилы в формате Agent Skills (https://agentskills.io/specification): любой скил
оттуда — SKILL.md + references/ + scripts/ — кладётся в skills/ и работает."""
import json
from pathlib import Path

import pytest

from app.agents.loader import compose_prompt
from app.agents.messages import ConfirmationRequest
from app.skills.loader import SPEC_KEYS, load_all_skills, load_skill, parse_frontmatter, resource_files, validate
from app.skills.resources import build_resource_tools
from app.tools.base import Safety

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def _standard_skill(root: Path, name: str = "pdf-notes", frontmatter: str = "") -> Path:
    d = root / name
    (d / "references").mkdir(parents=True)
    (d / "scripts").mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Разбор заметок. Use when the user mentions notes.\n"
        f"license: Apache-2.0\n{frontmatter}---\n\n# Заметки\n\nСм. references/guide.md, "
        "запусти scripts/count.py.\n", encoding="utf-8")
    (d / "references" / "guide.md").write_text("как разбирать заметки", encoding="utf-8")
    (d / "scripts" / "count.py").write_text(
        "import pathlib, sys\nprint(len(sys.argv) - 1, pathlib.Path('references/guide.md').exists())\n",
        encoding="utf-8")
    return d


def test_every_library_skill_follows_the_standard():
    dirs = [d for d in SKILLS_DIR.iterdir() if (d / "SKILL.md").exists()]
    loaded = load_all_skills(SKILLS_DIR)
    assert sorted(loaded) == sorted(d.name for d in dirs)
    for d in dirs:
        meta, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
        assert set(meta) <= SPEC_KEYS, d.name
        assert len(meta["description"]) <= 1024, d.name
        assert "<" not in meta["description"] and ">" not in meta["description"], d.name
        # описание — единственное, по чему Директор выбирает навык: что делает и когда
        assert "когда" in meta["description"].lower(), d.name


@pytest.mark.parametrize("meta, problem", [
    ({"name": "x-y", "description": "d", "mcp": {"url": "u"}}, "metadata"),
    ({"name": "x-y", "description": "d", "untrusted": True}, "metadata"),
    ({"name": "x_y", "description": "d"}, "name"),
    ({"name": "Other", "description": "d"}, "name"),
    ({"name": "other", "description": "d"}, "каталогом"),
    ({"name": "x-y", "description": ""}, "description"),
    ({"name": "x-y", "description": "d", "metadata": {"untrusted": True}}, "metadata"),
])
def test_validate_rejects_non_standard_frontmatter(meta, problem):
    assert problem in validate(meta, "x-y")


def test_what_claude_code_forgives_is_accepted():
    """Как у Anthropic claude-api: описание 1068 символов, лишнее поле."""
    assert validate({"name": "x-y", "description": "d" * 1068, "version": "1"}, "x-y") is None


def test_invalid_skill_is_skipped_not_fatal(tmp_path):
    _standard_skill(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: bad\ndescription: d\nsafety: safe\n---\n")
    assert list(load_all_skills(tmp_path)) == ["pdf-notes"]


@pytest.mark.parametrize("frontmatter", [
    "name: bad\ndescription: [незакрытая\n",  # yaml.YAMLError, а не ValueError
    "- name\n- bad\n",                          # YAML цел, но это список, а не поля
])
def test_broken_frontmatter_is_skipped_not_fatal(tmp_path, frontmatter):
    _standard_skill(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(f"---\n{frontmatter}---\n", encoding="utf-8")
    assert list(load_all_skills(tmp_path)) == ["pdf-notes"]


def test_standard_skill_loads_as_playbook_with_resources(tmp_path):
    skill = load_skill(_standard_skill(tmp_path))
    assert skill.tools == [] and not skill.has_code
    assert resource_files(skill) == ["references/guide.md", "scripts/count.py"]
    prompt = compose_prompt("роль", [skill])
    assert "# Заметки" in prompt and "references/guide.md, scripts/count.py" in prompt


async def test_agent_reads_only_granted_skill_files(tmp_path):
    skill = load_skill(_standard_skill(tmp_path))
    tools = {t.name: t for t in build_resource_tools([skill])}
    read = tools["read_skill_file"]
    assert read.safety is Safety.SAFE
    ok = json.loads(await read.execute({"skill": "pdf-notes", "path": "references/guide.md"}))
    assert ok["content"] == "как разбирать заметки"
    for path in ("SKILL.md", "../../etc/passwd", "references/../SKILL.md", "/etc/passwd"):
        assert "error" in json.loads(await read.execute({"skill": "pdf-notes", "path": path}))
    assert "error" in json.loads(await read.execute({"skill": "db", "path": "agent_ro.sql"}))


async def test_skill_script_needs_confirmation_and_runs_from_skill_root(tmp_path):
    skill = load_skill(_standard_skill(tmp_path))
    run = {t.name: t for t in build_resource_tools([skill])}["run_skill_script"]
    assert run.safety is Safety.DANGEROUS
    out = json.loads(await run.invoke({"skill": "pdf-notes", "script": "scripts/count.py", "args": ["a", "b"]}))
    assert out["returncode"] == 0 and out["stdout"].strip() == "2 True"
    refused = json.loads(await run.invoke({"skill": "pdf-notes", "script": "references/guide.md", "args": []}))
    assert "error" in refused


def test_yes_to_all_for_script_is_scoped_to_that_script():
    req = ConfirmationRequest(run_id="r", agent_id="a", tool_call_id="c", tool_name="run_skill_script",
                              args={"skill": "pdf-notes", "script": "scripts/count.py", "args": ["x"]})
    assert req.scope() == "run_skill_script: skill=pdf-notes, script=scripts/count.py"


def test_scripts_of_a_skill_with_code_stay_behind_its_tools():
    remnawave = load_skill(SKILLS_DIR / "remnawave")
    assert resource_files(remnawave) == ["references/api.md"]
    assert [t.name for t in build_resource_tools([remnawave])] == ["read_skill_file"]


async def test_untrusted_skill_with_scripts_is_not_granted(tmp_path):
    from app.agents.director import Director

    skill = load_skill(_standard_skill(tmp_path, frontmatter='metadata:\n  untrusted: "true"\n'))
    d = Director(llm=None, skills={skill.name: skill})
    spawn = next(t for t in d.tools if t.name == "spawn")
    out = await spawn.fn(role="r", skills=[skill.name], task="t")
    assert "недоверенный" in out["error"]


async def test_subscription_is_untrusted_and_refused_next_to_host():
    """Скил тянет чужой текст: имена узлов подписки и ответы ip-api."""
    from app.agents.director import Director

    skills = load_all_skills(SKILLS_DIR)
    assert skills["subscription"].untrusted is True
    d = Director(llm=None, skills=skills)
    spawn = next(t for t in d.tools if t.name == "spawn")
    out = await spawn.fn(role="r", skills=["subscription", "host"], task="t")
    assert "недоверенный" in out["error"]


def test_untrusted_tools_py_is_not_executed(tmp_path):
    """Чужой навык с кодом: tools.py не импортируется, остальное работает."""
    d = _standard_skill(tmp_path)
    marker = tmp_path / "executed"
    (d / "tools.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    skill = load_skill(d)
    assert not marker.exists()
    assert skill.tools == [] and not skill.has_code
    assert "scripts/count.py" in resource_files(skill)
    assert [t.name for t in build_resource_tools([skill])] == ["read_skill_file", "run_skill_script"]


def test_every_library_skill_with_code_is_trusted():
    from app.skills.loader import trusted_code

    with_code = {d.name for d in SKILLS_DIR.iterdir() if (d / "tools.py").exists()}
    assert with_code <= trusted_code(SKILLS_DIR)
    assert all(s.has_code for s in load_all_skills(SKILLS_DIR).values() if s.name in with_code)


def test_trusted_list_skips_comments_and_blanks(tmp_path):
    from app.skills.loader import trusted_code

    assert trusted_code(tmp_path) == frozenset()
    (tmp_path / "TRUSTED").write_text("# свои\nhost  # хост\n\ndb\n", encoding="utf-8")
    assert trusted_code(tmp_path) == {"host", "db"}


async def test_host_access_declared_in_frontmatter_without_code(tmp_path):
    from app.skills.readonly import build_host_tools

    skill = load_skill(_standard_skill(tmp_path, frontmatter=(
        'metadata:\n  host-binaries: "df uptime"\n  host-exec: "true"\n')))
    assert not skill.has_code
    assert skill.access.binaries == {"df", "uptime"} and skill.access.exec_allowed
    tools = {t.name: t for t in build_host_tools(skill.access)}
    assert tools["host_query"].safety is Safety.SAFE
    assert tools["shell_exec"].safety is Safety.DANGEROUS
    refused = json.loads(await tools["host_query"].execute({"command": ["rm", "-rf", "/"]}))
    assert "error" in refused



# ---------- выученные навыки: том, только текст ----------

def _learned(root: Path, name: str, frontmatter: str = "", body: str = "шаги") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: когда просят {name}\n"
                                f"{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return d


def test_learned_skill_loads_next_to_library_as_text_only(tmp_path):
    _standard_skill(tmp_path / "library")
    _learned(tmp_path / "learned", "weekly-report")
    skills = load_all_skills(tmp_path / "library", tmp_path / "learned")
    assert sorted(skills) == ["pdf-notes", "weekly-report"]
    learned = skills["weekly-report"]
    assert learned.tools == [] and not learned.has_code
    assert learned.access.binaries == frozenset() and not learned.access.exec_allowed


@pytest.mark.parametrize("extra", ["metadata", "tools.py", "scripts"])
def test_learned_skill_cannot_grant_access_or_run_code(tmp_path, extra):
    """Каталог выученных пишет процесс, читающий недоверенный текст: доступ к хосту
    (metadata.host-exec), MCP, tools.py и scripts/ — только у навыков владельца."""
    marker = tmp_path / "executed"
    d = _learned(tmp_path / "learned", "sneaky",
                 frontmatter='metadata:\n  host-exec: "true"\n' if extra == "metadata" else "")
    if extra == "tools.py":
        (d / "tools.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    if extra == "scripts":
        (d / "scripts").mkdir()
        (d / "scripts" / "x.sh").write_text("id\n", encoding="utf-8")
    assert load_all_skills(tmp_path / "library", tmp_path / "learned") == {}
    assert not marker.exists()


def test_library_wins_over_learned_skill_with_the_same_name(tmp_path):
    _standard_skill(tmp_path / "library")
    _learned(tmp_path / "learned", "pdf-notes", body="переписанное моделью")
    skills = load_all_skills(tmp_path / "library", tmp_path / "learned")
    assert "переписанное моделью" not in skills["pdf-notes"].instructions


def test_missing_learned_dir_is_empty(tmp_path):
    _standard_skill(tmp_path / "library")
    assert list(load_all_skills(tmp_path / "library", tmp_path / "nope")) == ["pdf-notes"]
