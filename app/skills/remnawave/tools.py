import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, Field

from app.tools.base import Tool, Safety
from app.config import settings

_SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Каталог скриптов. Агент видит только эти имена, а не весь OpenAPI.
_READ_SCRIPTS = {"user-find", "user-get", "user-traffic", "user-devices", "nodes"}
_ACTION_SCRIPTS = {
    "user-extend", "user-enable", "user-disable",
    "user-revoke", "user-reset-traffic", "hwid-reset",
}


class ScriptParams(BaseModel):
    script: str = Field(description="имя скрипта, напр. user-find")
    args: list[str] = Field(default_factory=list, description="позиционные аргументы скрипта")


async def _run_script(script: str, args: list[str]) -> dict:
    path = _SCRIPTS_DIR / f"{script}.sh"
    env = {
        **os.environ,
        "REMNAWAVE_BASE_URL": settings.remnawave_base_url,
        "REMNAWAVE_API_KEY": settings.remnawave_api_key,
        "REMNAWAVE_TIMEOUT": str(settings.remnawave_timeout),
    }
    proc = await asyncio.create_subprocess_exec(
        "bash", str(path), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.remnawave_timeout + 5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"script": script, "error": f"таймаут ({settings.remnawave_timeout}s)"}
    return {
        "script": script,
        "returncode": proc.returncode,
        "stdout": out.decode(errors="replace").strip(),
        "stderr": err.decode(errors="replace").strip(),
    }


async def rw_query(script: str, args: list[str]) -> dict:
    if script not in _READ_SCRIPTS:
        return {
            "error": f"{script!r} — не read-скрипт; для мутаций используй rw_action (с подтверждением)",
            "read_scripts": sorted(_READ_SCRIPTS),
        }
    return await _run_script(script, args)


async def rw_action(script: str, args: list[str]) -> dict:
    if script not in _ACTION_SCRIPTS:
        return {
            "error": f"{script!r} — не мутирующий скрипт",
            "action_scripts": sorted(_ACTION_SCRIPTS),
        }
    return await _run_script(script, args)


def build_tools() -> list[Tool]:
    return [
        Tool("rw_query", "Run a READ-ONLY remnawave panel script (user-find, user-get, user-traffic, user-devices, nodes). Safe, auto-executed.", ScriptParams, rw_query, Safety.SAFE),
        Tool("rw_action", "Run a remnawave MUTATION script (user-extend, user-enable, user-disable, user-revoke, user-reset-traffic, hwid-reset). DESTRUCTIVE, changes a paying customer's subscription. Requires user confirmation.", ScriptParams, rw_action, Safety.DANGEROUS),
    ]
