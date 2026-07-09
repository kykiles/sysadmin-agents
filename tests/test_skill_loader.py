from app.skills.loader import parse_frontmatter
from app.skills.docker.tools import build_tools as build_docker_tools
from app.tools.base import Safety


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
