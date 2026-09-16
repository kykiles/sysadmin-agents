"""Файлы скила по стандарту Agent Skills: references/ и assets/ агент читает по
запросу, scripts/ запускает с подтверждением.

Инструменты собираются при спавне из выданных навыков — как host_query: агент не
видит файлов скилов, которых ему не выдали. Скрипты чужого скила — чужой код в
контейнере с docker socket и доступом к хосту, поэтому запуск только DANGEROUS.
"""
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from app.skills.loader import Skill, resource_files
from app.tools.base import Safety, Tool
from app.tools.docker import _run_subprocess

# Файл целиком уходит в контекст; больше — это уже не справочник, а данные.
MAX_READ_CHARS = 60_000
# Чем запускать скрипт; остальное — только исполняемый файл с shebang.
_INTERPRETERS = {".py": [sys.executable], ".sh": ["bash"]}


class ReadSkillFileParams(BaseModel):
    skill: str = Field(description="имя выданного навыка")
    path: str = Field(description="путь от корня навыка, как в его SKILL.md, напр. references/api.md")


class RunSkillScriptParams(BaseModel):
    skill: str = Field(description="имя выданного навыка")
    script: str = Field(description="путь скрипта от корня навыка, напр. scripts/extract.py")
    args: list[str] = Field(default_factory=list, description="аргументы скрипта")


def _resolve(skills: dict[str, Skill], skill: str, rel: str, allowed: set[str]) -> Path | str:
    """Абсолютный путь файла или причина отказа."""
    if skill not in skills:
        return f"навык {skill} не выдан этому агенту"
    if rel not in allowed:
        return f"в навыке {skill} нет файла {rel}; доступны: {sorted(allowed)}"
    return skills[skill].path / rel


def build_resource_tools(chosen: list[Skill]) -> list[Tool]:
    skills = {s.name: s for s in chosen}
    files = {s.name: set(resource_files(s)) for s in chosen}
    scripts = {n: {f for f in fs if f.startswith("scripts/")} for n, fs in files.items()}
    tools: list[Tool] = []

    async def read_skill_file(skill: str, path: str) -> dict:
        target = _resolve(skills, skill, path, files.get(skill, set()))
        if isinstance(target, str):
            return {"error": target}
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return {"skill": skill, "path": path, "content": text[:MAX_READ_CHARS],
                    "truncated": f"показано {MAX_READ_CHARS} из {len(text)} символов"}
        return {"skill": skill, "path": path, "content": text}

    async def run_skill_script(skill: str, script: str, args: list[str]) -> dict:
        target = _resolve(skills, skill, script, scripts.get(skill, set()))
        if isinstance(target, str):
            return {"error": target}
        argv = [*_INTERPRETERS.get(target.suffix, []), str(target), *args]
        return {"skill": skill, "script": script,
                **await _run_subprocess(argv, cwd=str(skills[skill].path))}

    if any(files.values()):
        tools.append(Tool(
            "read_skill_file",
            "Read a file of a granted skill (references/, assets/, scripts/) when its SKILL.md "
            "points to it. Safe.",
            ReadSkillFileParams, read_skill_file, Safety.SAFE,
        ))
    if any(scripts.values()):
        tools.append(Tool(
            "run_skill_script",
            "Run a script from a granted skill's scripts/ with arguments, working directory — "
            "the skill root. Third-party code: requires user confirmation.",
            RunSkillScriptParams, run_skill_script, Safety.DANGEROUS,
        ))
    return tools
