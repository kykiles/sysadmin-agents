from pathlib import Path
from unittest.mock import Mock

from app.skills.loader import load_all_skills
from app.agents.loader import load_agents
from app.agents.registry import AgentRegistry


def test_specialists_get_memory_tools():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    skills = load_all_skills(app_dir / "skills")
    registry = AgentRegistry()
    load_agents(app_dir / "agents" / "defs", skills, llm=Mock(), registry=registry)

    for name in ("dbadmin", "dockeradmin", "hostadmin"):
        agent = registry.get_agent(name)
        tool_names = {t.name for t in agent.tools}
        assert {"recall_facts", "remember_fact", "forget_fact"} <= tool_names
