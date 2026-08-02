from app.agents.director import Director
from app.skills.loader import Skill, load_skill


def _skill(name: str, description: str) -> Skill:
    return Skill(name=name, description=description, instructions="плейбук", tools=[])


def test_skill_without_tools_py_is_a_plain_playbook(tmp_path):
    d = tmp_path / "writing"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: writing\ndescription: пишет посты\n---\n\n## Навык: письмо\nПиши коротко.",
        encoding="utf-8",
    )
    skill = load_skill(d)
    assert skill.tools == []
    assert "Пиши коротко." in skill.instructions


def test_reload_picks_up_a_new_skill():
    d = Director(llm=None, skills={"host": _skill("host", "хост")})
    assert "host" in d._library and "tls" not in d._library

    d.reload_library({"host": _skill("host", "хост"), "tls": _skill("tls", "сертификаты")})

    assert "tls" in d._library
    assert "сертификаты" in d._base_prompt  # навык виден Директору в списке доступных


def test_reload_keeps_memory_away_from_spawned_agents():
    d = Director(llm=None, skills={"host": _skill("host", "хост")})
    d.reload_library({"host": _skill("host", "хост"), "memory": _skill("memory", "факты")})
    assert "memory" not in d._library
