from pathlib import Path

import pytest

from app.agents.loader import load_agents
from app.agents.registry import AgentRegistry
from app.skills.loader import load_all_skills

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "app" / "skills"
DEFS_DIR = ROOT / "app" / "agents" / "defs"


class FakeLLM:
    async def chat(self, messages, tools=None):
        raise NotImplementedError


def test_load_agents_composes_prompt_and_tools():
    skills = load_all_skills(SKILLS_DIR)
    reg = AgentRegistry()
    available = load_agents(DEFS_DIR, skills, FakeLLM(), reg)

    assert available["dockeradmin"]
    agent = reg.get_agent("dockeradmin")
    assert "docker_ps" in {t.name for t in agent.tools}
    # системный промпт содержит и роль, и подмешанный плейбук skill'а
    assert "управление Docker" in agent.system_prompt


def test_deployadmin_agent_registered():
    skills = load_all_skills(SKILLS_DIR)
    reg = AgentRegistry()
    available = load_agents(DEFS_DIR, skills, FakeLLM(), reg)
    assert "deployadmin" in available
    assert "deploy_run" in {t.name for t in reg.get_agent("deployadmin").tools}


def test_new_specialist_agents_registered():
    skills = load_all_skills(SKILLS_DIR)
    reg = AgentRegistry()
    available = load_agents(DEFS_DIR, skills, FakeLLM(), reg)
    for name, tool in [
        ("observer", "observe_query"),
        ("backupadmin", "backup_run"),
        ("secadmin", "sec_query"),
        ("remnawave", "rw_action"),
    ]:
        assert name in available
        assert tool in {t.name for t in reg.get_agent(name).tools}


def test_load_agents_unknown_skill_raises(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("---\nname: bad\ndescription: x\nskills:\n  - nope\n---\nроль", encoding="utf-8")
    with pytest.raises(ValueError, match="nope"):
        load_agents(tmp_path, {}, FakeLLM(), AgentRegistry())
