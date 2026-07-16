from pathlib import Path

from app.agents.base import Agent
from app.agents.registry import AgentRegistry
from app.llm.client import LLMClient
from app.skills.loader import Skill, parse_frontmatter


# Общие правила эффективности для всех специалистов: число шагов ограничено, поэтому
# независимые проверки надо объединять и не повторять уже выполненное.
_EFFICIENCY_RULES = (
    "Правила работы (важно — число шагов ограничено):\n"
    "- Объединяй независимые проверки в один вызов: несколько read-only команд можно "
    "выполнить разом через `sh -c 'A && B && C'` (пайпы, циклы, 2>/dev/null допустимы — "
    "это остаётся без подтверждения, если все команды внутри read-only).\n"
    "- Не повторяй команду, которую уже выполнял в этом диалоге: используй прошлый вывод.\n"
    "- Если для задачи есть готовый агрегирующий инструмент (например tls_report) — "
    "начни с него, а точечные команды оставь на доуточнение."
)


def _compose_prompt(role: str, skills: list[Skill]) -> str:
    parts = [role.strip(), _EFFICIENCY_RULES]
    for skill in skills:
        parts.append(skill.instructions)
    return "\n\n".join(parts)


def load_agents(
    defs_dir: Path,
    skills: dict[str, Skill],
    llm: LLMClient,
    registry: AgentRegistry,
) -> dict[str, str]:
    available: dict[str, str] = {}
    for f in sorted(defs_dir.glob("*.md")):
        meta, role = parse_frontmatter(f.read_text(encoding="utf-8"))
        name = meta["name"]
        chosen: list[Skill] = []
        for sn in meta.get("skills", []):
            if sn not in skills:
                raise ValueError(f"agent {name}: неизвестный skill {sn}")
            chosen.append(skills[sn])
        agent = Agent(
            name=name,
            system_prompt=_compose_prompt(role, chosen),
            tools=[t for s in chosen for t in s.tools],
            llm=llm,
            registry=registry,
        )
        registry.register(agent)
        available[name] = meta["description"]
    return available
