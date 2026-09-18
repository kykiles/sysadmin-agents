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
