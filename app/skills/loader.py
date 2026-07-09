import importlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.tools.base import Tool


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    tools: list[Tool]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    return meta, body


def load_skill(skill_dir: Path) -> Skill:
    meta, body = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    mod = importlib.import_module(f"app.skills.{skill_dir.name}.tools")
    if not hasattr(mod, "build_tools"):
        raise ValueError(f"skill {skill_dir.name}: tools.py должен определять build_tools()")
    return Skill(
        name=meta["name"],
        description=meta["description"],
        instructions=body.strip(),
        tools=mod.build_tools(),
    )


def load_all_skills(root: Path) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for d in sorted(root.iterdir()):
        if (d / "SKILL.md").exists():
            skill = load_skill(d)
            skills[skill.name] = skill
    return skills
