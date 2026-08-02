import importlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.skills.readonly import HostAccess
from app.tools.base import Tool


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    tools: list[Tool]
    # Доступ к хосту, который скил приносит агенту. При спавне доступы выданных
    # скилов объединяются в один host_query — см. readonly.build_host_tools.
    access: HostAccess = field(default_factory=HostAccess)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    return meta, body


def load_skill(skill_dir: Path) -> Skill:
    meta, body = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    tools: list[Tool] = []
    access = HostAccess()
    # tools.py необязателен: скилл может быть чистым плейбуком поверх инструментов
    # других скиллов (например «как писать пост» поверх shell'а). А если есть —
    # он даёт свои инструменты, доступ к хосту (ACCESS) или и то, и другое.
    if (skill_dir / "tools.py").exists():
        mod = importlib.import_module(f"app.skills.{skill_dir.name}.tools")
        access = getattr(mod, "ACCESS", access)
        if hasattr(mod, "build_tools"):
            tools = mod.build_tools()
        elif not access.binaries and not access.exec_allowed:
            raise ValueError(
                f"skill {skill_dir.name}: tools.py должен определять build_tools() или ACCESS"
            )
    return Skill(
        name=meta["name"],
        description=meta["description"],
        instructions=body.strip(),
        tools=tools,
        access=access,
    )


def load_all_skills(root: Path) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for d in sorted(root.iterdir()):
        if (d / "SKILL.md").exists():
            skill = load_skill(d)
            skills[skill.name] = skill
    return skills
