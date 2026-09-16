from app.skills.loader import Skill, resource_files


# Общие правила эффективности для всех специалистов: число шагов ограничено, поэтому
# независимые проверки надо объединять и не повторять уже выполненное.
_EFFICIENCY_RULES = (
    "Правила работы (важно — число шагов ограничено):\n"
    "- Объединяй независимые проверки в один ход: несколько read-only вызовов в одном "
    "ответе выполняются параллельно. Каждый вызов — одна команда argv; `sh -c`, пайпы "
    "и редиректы без подтверждения не выполняются.\n"
    "- Не повторяй вызов, который уже делал в этом диалоге: используй прошлый вывод.\n"
    "- Если для задачи есть готовый агрегирующий инструмент — начни с него, "
    "а точечные вызовы оставь на доуточнение."
)


def compose_prompt(role: str, skills: list[Skill]) -> str:
    parts = [role.strip(), _EFFICIENCY_RULES]
    for skill in skills:
        parts.append(skill.instructions)
        # Ссылки в SKILL.md идут от корня навыка; сами файлы — по запросу, не в промпт.
        if files := resource_files(skill):
            parts.append(f"Файлы навыка `{skill.name}` (read_skill_file / run_skill_script): "
                         + ", ".join(files))
    return "\n\n".join(parts)
