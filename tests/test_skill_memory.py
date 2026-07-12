from pathlib import Path

from app.skills.loader import load_all_skills


def test_memory_skill_is_loaded_with_three_tools():
    root = Path(__file__).resolve().parent.parent / "app" / "skills"
    skills = load_all_skills(root)
    assert "memory" in skills
    tool_names = {t.name for t in skills["memory"].tools}
    assert tool_names == {"recall_facts", "remember_fact", "forget_fact"}
