import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.logging import get_logger
from app.skills import mcp_bridge
from app.skills.readonly import HostAccess
from app.tools.base import Safety, Tool

log = get_logger("skills")

# Формат Agent Skills (https://agentskills.io/specification) — тот же, что у скилов
# Claude: любой скил оттуда кладётся в skills/ как есть. Свои поля — только в metadata.
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
# Наши прежние поля в корне: молча проигнорировать их нельзя — скил потерял бы
# untrusted или стал бы безопаснее, чем задумал автор.
_LEGACY_KEYS = {"mcp", "safety", "untrusted"}
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# Каталоги ресурсов по стандарту. Агент читает их по запросу, а не получает в промпт.
RESOURCE_DIRS = ("references", "assets", "scripts")


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    tools: list[Tool]
    # Доступ к хосту, который скил приносит агенту. При спавне доступы выданных
    # скилов объединяются в один host_query — см. readonly.build_host_tools.
    access: HostAccess = field(default_factory=HostAccess)
    # Инструменты, которым нужен объединённый доступ агента: у ssh список read-only
    # бинарников зависит от того, какие скилы выданы вместе с транспортом.
    access_tools: Callable[[HostAccess], list[Tool]] | None = None
    # Вывод скила содержит текст, который мы не контролируем (веб-страница, чужой
    # API). Такому агенту нельзя одновременно давать полномочия — см. Director._spawn.
    untrusted: bool = False
    # Каталог скила: отсюда агент читает references/ и assets/, запускает scripts/.
    path: Path | None = None
    # tools.py — наш код: скрипты такого скила вызывают его инструменты, а не агент.
    has_code: bool = False


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    return meta, body


def validate(meta: dict, dir_name: str) -> str | None:
    """Нарушение, с которым скил не загрузить, или None.

    Лишь то, без чего скил работает неправильно. Длинное описание или незнакомое поле
    Claude Code прощает — прощаем и мы: у Anthropic есть скилы с описанием длиннее
    1024. Свою библиотеку по спецификации строго проверяет тест.
    """
    if legacy := _LEGACY_KEYS & set(meta):
        return f"поля {sorted(legacy)} переехали в metadata: mcp-url, mcp-safety, untrusted"
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name):
        return "name: 1-64 символа, строчные латинские буквы, цифры и дефисы"
    if name != dir_name:
        return f"name {name!r} не совпадает с каталогом {dir_name!r}"
    if not isinstance(description, str) or not description.strip():
        return "description: непустая строка"
    metadata = meta.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, dict) or not all(isinstance(v, str) for v in metadata.values())
    ):
        return "metadata: словарь строка → строка"
    return None


def load_skill(skill_dir: Path) -> Skill:
    meta, body = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    if problem := validate(meta, skill_dir.name):
        raise ValueError(f"skill {skill_dir.name}: {problem}")
    ext = meta.get("metadata") or {}
    tools: list[Tool] = []
    access = HostAccess()
    access_tools = None
    # Инструменты MCP-сервера приходят снаружи, и уровень риска у них взять неоткуда:
    # сервер отдаёт только имя, описание и схему. Решает человек, подключивший сервер,
    # полем `metadata.mcp-safety: safe`; молчание означает «опасно» — единственный
    # безопасный дефолт для чужого кода.
    if ext.get("mcp-url"):
        tools = mcp_bridge.build_tools(
            {"url": ext["mcp-url"]},
            Safety.SAFE if ext.get("mcp-safety") == "safe" else Safety.DANGEROUS,
            skill_dir.name,
        )
    # tools.py необязателен: скилл может быть чистым плейбуком поверх инструментов
    # других скиллов (например «как писать пост» поверх shell'а). А если есть —
    # он даёт свои инструменты, доступ к хосту (ACCESS) или и то, и другое.
    has_code = (skill_dir / "tools.py").exists()
    if has_code:
        # Скилы лежат вне пакета ядра (корневой skills/) — домен не должен быть частью
        # app/. Имя пакета берём из каталога, который нам дали: так загрузчик не знает
        # заранее, где живёт библиотека.
        mod = importlib.import_module(f"{skill_dir.parent.name}.{skill_dir.name}.tools")
        access = getattr(mod, "ACCESS", access)
        access_tools = getattr(mod, "build_access_tools", None)
        if hasattr(mod, "build_tools"):
            tools = mod.build_tools()
        elif not access_tools and not access.binaries and not access.exec_allowed:
            raise ValueError(
                f"skill {skill_dir.name}: tools.py должен определять build_tools() или ACCESS"
            )
    return Skill(
        name=meta["name"],
        description=meta["description"],
        instructions=body.strip(),
        tools=tools,
        access=access,
        access_tools=access_tools,
        untrusted=ext.get("untrusted") == "true",
        path=skill_dir,
        has_code=has_code,
    )


def resource_files(skill: Skill) -> list[str]:
    """Файлы из каталогов ресурсов, пути от корня скила — как на них ссылается SKILL.md."""
    if skill.path is None:
        return []
    return sorted(
        f.relative_to(skill.path).as_posix()
        for d in RESOURCE_DIRS if (skill.path / d).is_dir()
        for f in (skill.path / d).rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
        # скрипты скила с кодом вызывают его инструменты — агенту их не показываем
        and not (skill.has_code and d == "scripts")
    )


def load_all_skills(root: Path) -> dict[str, Skill]:
    """Невалидный скил пропускается с записью в лог: скачанный чужой скил не должен
    ронять запуск бота и /reload."""
    skills: dict[str, Skill] = {}
    for d in sorted(root.iterdir()):
        if (d / "SKILL.md").exists():
            try:
                skill = load_skill(d)
            except ValueError as e:
                log.error("skill_invalid", skill=d.name, error=str(e))
                continue
            skills[skill.name] = skill
    return skills
