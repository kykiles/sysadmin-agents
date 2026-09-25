import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.logging import get_logger
from app.skills import mcp_bridge
from app.skills.readonly import KNOWN_BINARIES, HostAccess
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
    """Любая порча фронтматтера — ValueError: yaml бросает своё YAMLError, и навык
    с битым YAML ронял load_all_skills, а на старте — весь бот циклом рестартов."""
    if not text.startswith("---"):
        return {}, text
    try:
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
    except (ValueError, yaml.YAMLError) as e:
        raise ValueError(f"фронтматтер не разобран: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError("фронтматтер — не набор полей «имя: значение»")
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


def trusted_code(root: Path) -> frozenset[str]:
    """Навыки, чей tools.py разрешено исполнять: по имени на строку в `root/TRUSTED`.

    tools.py исполняется при импорте — до всяких пометок SAFE/DANGEROUS, поэтому
    чужой навык с кодом получил бы полное доверие, просто оказавшись в каталоге.
    Список ведёт владелец, а не автор навыка: поле в metadata автор выставил бы себе сам.
    """
    f = root / "TRUSTED"
    if not f.exists():
        return frozenset()
    lines = (line.split("#", 1)[0].strip() for line in f.read_text(encoding="utf-8").splitlines())
    return frozenset(line for line in lines if line)


def _declared_access(name: str, ext: dict) -> HostAccess:
    """Доступ к хосту из frontmatter: навыку без кода не нужен tools.py ради ACCESS.
    Что из объявленного читает без подтверждения, по-прежнему решает readonly.py."""
    binaries = frozenset(ext.get("host-binaries", "").split())
    if unknown := binaries - KNOWN_BINARIES:
        log.warning("skill_unknown_binaries", skill=name, binaries=sorted(unknown))
    return HostAccess(binaries=binaries, exec_allowed=ext.get("host-exec") == "true")


def load_skill(skill_dir: Path) -> Skill:
    meta, body = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    if problem := validate(meta, skill_dir.name):
        raise ValueError(f"skill {skill_dir.name}: {problem}")
    ext = meta.get("metadata") or {}
    tools: list[Tool] = []
    access = _declared_access(skill_dir.name, ext)
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
    if has_code and skill_dir.name not in trusted_code(skill_dir.parent):
        # Навык остаётся плейбуком со скриптами (они с подтверждением), код ждёт
        # владельца. write_skill его всё равно не тронет — он смотрит на файл.
        log.warning("skill_code_untrusted", skill=skill_dir.name)
        has_code = False
    if has_code:
        # Скилы лежат вне пакета ядра (корневой skills/) — домен не должен быть частью
        # app/. Имя пакета берём из каталога, который нам дали: так загрузчик не знает
        # заранее, где живёт библиотека.
        mod = importlib.import_module(f"{skill_dir.parent.name}.{skill_dir.name}.tools")
        access = access | getattr(mod, "ACCESS", HostAccess())
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


def load_learned(skill_dir: Path) -> Skill:
    """Выученный навык — ровно то, что пишет write_skill: SKILL.md с name и
    description. Через metadata навык выдаёт доступ к хосту и подключает MCP, код и
    scripts/ исполняются — это всё у навыков владельца, а каталог выученных пишет
    процесс, который читает недоверенный текст."""
    meta, body = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    if problem := validate(meta, skill_dir.name):
        raise ValueError(f"skill {skill_dir.name}: {problem}")
    extra = ["metadata"] if "metadata" in meta else []
    extra += [p for p in ("tools.py", *RESOURCE_DIRS) if (skill_dir / p).exists()]
    if extra:
        raise ValueError(f"skill {skill_dir.name}: выученный навык — только текст, "
                         f"а здесь {', '.join(extra)}")
    return Skill(name=meta["name"], description=meta["description"],
                 instructions=body.strip(), tools=[], path=skill_dir)


def _load_dir(root: Path, load: Callable[[Path], Skill]) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    if not root.is_dir():
        return skills
    for d in sorted(root.iterdir()):
        if (d / "SKILL.md").exists():
            try:
                skill = load(d)
            except ValueError as e:
                log.error("skill_invalid", skill=d.name, error=str(e))
                continue
            skills[skill.name] = skill
    return skills


def load_all_skills(root: Path, learned: Path | None = None) -> dict[str, Skill]:
    """Библиотека владельца (`root`) и выученные навыки (`learned`, их пишет
    write_skill). Невалидный скил пропускается с записью в лог: скачанный чужой скил
    не должен ронять запуск бота и write_skill. Имя из библиотеки выученный навык не
    перекрывает — решение человека главнее."""
    skills = _load_dir(root, load_skill)
    if learned is not None:
        for name, skill in _load_dir(learned, load_learned).items():
            if name in skills:
                log.warning("learned_skill_shadowed", skill=name)
                continue
            skills[name] = skill
    return skills
