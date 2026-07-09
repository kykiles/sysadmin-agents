from pathlib import Path

from app.skills.loader import parse_frontmatter, Skill, load_skill, load_all_skills
from app.skills.docker.tools import build_tools as build_docker_tools
from app.tools.base import Safety

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "app" / "skills"


def test_parse_frontmatter_extracts_meta_and_body():
    text = "---\nname: docker\ndescription: управление docker\n---\nТело плейбука\n"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "docker", "description": "управление docker"}
    assert body.strip() == "Тело плейбука"


def test_parse_frontmatter_no_frontmatter_returns_empty_meta():
    meta, body = parse_frontmatter("просто текст")
    assert meta == {}
    assert body == "просто текст"


def test_docker_skill_tools():
    tools = build_docker_tools()
    by_name = {t.name: t for t in tools}
    assert {"docker_ps", "docker_logs", "docker_restart", "compose_up"} <= set(by_name)
    assert by_name["docker_ps"].safety is Safety.SAFE
    assert by_name["docker_restart"].safety is Safety.DANGEROUS
    # docker_query и shell_exec принадлежат другим skill'ам
    assert "docker_query" not in by_name
    assert "shell_exec" not in by_name


def test_load_skill_docker():
    skill = load_skill(SKILLS_DIR / "docker")
    assert isinstance(skill, Skill)
    assert skill.name == "docker"
    assert skill.description
    assert "управление Docker" in skill.instructions
    assert "docker_ps" in {t.name for t in skill.tools}


def test_load_all_skills_includes_docker():
    skills = load_all_skills(SKILLS_DIR)
    assert "docker" in skills
    assert isinstance(skills["docker"], Skill)


def test_db_and_host_skills_load():
    skills = load_all_skills(SKILLS_DIR)
    assert {"docker", "db", "host"} <= set(skills)
    db_tools = {t.name for t in skills["db"].tools}
    host_tools = {t.name: t for t in skills["host"].tools}
    assert db_tools == {"docker_query"}
    assert skills["db"].tools[0].safety is Safety.SAFE
    assert "shell_exec" in host_tools
    assert host_tools["shell_exec"].safety is Safety.DANGEROUS
