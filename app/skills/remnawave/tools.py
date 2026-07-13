import asyncio
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec
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


class CurlParams(BaseModel):
    method: str = Field(description="HTTP-метод: GET, POST, PATCH, DELETE")
    path: str = Field(description="путь эндпоинта панели, напр. /api/nodes (без хоста)")
    body: dict | None = Field(default=None, description="JSON-тело для POST/PATCH")


def _panel_url(path: str) -> str:
    # Домен зашит: агент задаёт только путь, база — из конфига. Никакого SSRF.
    return settings.remnawave_base_url.rstrip("/") + "/" + path.lstrip("/")


async def _curl(method: str, path: str, body: dict | None) -> dict:
    args = [
        "curl", "-sS", "--max-time", str(settings.remnawave_timeout),
        "-X", method.upper(),
        "-H", f"Authorization: Bearer {settings.remnawave_api_key}",
        "-H", "Content-Type: application/json",
        _panel_url(path),
    ]
    if body is not None:
        args += ["-d", json.dumps(body)]
    return await shell_exec(args)


async def rw_curl_read(method: str, path: str, body: dict | None = None) -> dict:
    if method.upper() != "GET":
        return {"error": f"{method} — мутация; используй rw_curl_write (с подтверждением)"}
    return await _curl("GET", path, body)


async def rw_curl_write(method: str, path: str, body: dict | None = None) -> dict:
    if method.upper() == "GET":
        return {"error": "GET — это чтение, используй rw_curl_read"}
    return await _curl(method, path, body)


def build_tools() -> list[Tool]:
    return [
        Tool("rw_query", "Run a READ-ONLY remnawave panel script (user-find, user-get, user-traffic, user-devices, nodes). Safe, auto-executed.", ScriptParams, rw_query, Safety.SAFE),
        Tool("rw_action", "Run a remnawave MUTATION script (user-extend, user-enable, user-disable, user-revoke, user-reset-traffic, hwid-reset). DESTRUCTIVE, changes a paying customer's subscription. Requires user confirmation.", ScriptParams, rw_action, Safety.DANGEROUS),
        Tool("rw_curl_read", "Raw GET to the remnawave panel API by PATH (host is fixed from config). Safe, auto-executed. Use for endpoints not covered by the ready scripts.", CurlParams, rw_curl_read, Safety.SAFE),
        Tool("rw_curl_write", "Raw POST/PATCH/DELETE to the remnawave panel API by PATH. DESTRUCTIVE, changes panel state. Requires user confirmation.", CurlParams, rw_curl_write, Safety.DANGEROUS),
    ]
