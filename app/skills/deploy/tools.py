import shlex
from pydantic import BaseModel, Field
from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec
from app.config import settings


class DeployParams(BaseModel):
    site: str = Field(description="имя каталога сайта под /opt")


def _allowed() -> set[str]:
    return {s.strip() for s in settings.deploy_allowed.split(",") if s.strip()}


def _valid_site(site: str) -> bool:
    return bool(site) and "/" not in site and ".." not in site


def _choose_method(has_script: bool, has_make_deploy: bool, has_compose: bool) -> tuple[str, str]:
    if has_script:
        return "script", "bash deployments/deploy.sh"
    if has_make_deploy:
        return "make", "make deploy"
    if has_compose:
        return "compose", "git pull --ff-only && docker compose up -d --build"
    return "none", ""


async def _host_run(script: str) -> dict:
    return await shell_exec(["nsenter", "-t", "1", "-m", "-p", "--", "bash", "-lc", script])


async def _detect(site_dir: str) -> tuple[str, str]:
    q = shlex.quote(site_dir)
    probe = (
        f"s=0; m=0; c=0; "
        f"[ -f {q}/deployments/deploy.sh ] && s=1; "
        f"grep -qE '^deploy:' {q}/Makefile 2>/dev/null && m=1; "
        f"{{ [ -f {q}/docker-compose.yml ] || [ -f {q}/compose.yaml ]; }} && c=1; "
        f"echo ${{s}}${{m}}${{c}}"
    )
    res = await _host_run(probe)
    flags = (res.get("stdout") or "").strip()[-3:]
    if len(flags) != 3:
        return "none", ""
    return _choose_method(flags[0] == "1", flags[1] == "1", flags[2] == "1")


async def deploy_plan(site: str) -> dict:
    if not _valid_site(site):
        return {"error": f"невалидное имя сайта: {site!r}"}
    if site not in _allowed():
        return {"error": f"сайт {site!r} не разрешён (DEPLOY_ALLOWED)", "allowed": sorted(_allowed())}
    site_dir = f"/opt/{site}"
    method, command = await _detect(site_dir)
    if method == "none":
        return {"site": site, "dir": site_dir, "method": "none",
                "error": "способ деплоя не определён (нет deploy.sh / make deploy / compose)"}
    return {"site": site, "dir": site_dir, "method": method, "command": command}


async def deploy_run(site: str) -> dict:
    if not _valid_site(site):
        return {"error": f"невалидное имя сайта: {site!r}"}
    if site not in _allowed():
        return {"error": f"сайт {site!r} не разрешён (DEPLOY_ALLOWED)", "allowed": sorted(_allowed())}
    site_dir = f"/opt/{site}"
    method, command = await _detect(site_dir)
    if method == "none":
        return {"site": site, "method": "none",
                "error": "способ деплоя не определён (нет deploy.sh / make deploy / compose)"}
    script = f"cd {shlex.quote(site_dir)} && {command}"
    res = await _host_run(script)
    return {
        "site": site,
        "method": method,
        "command": command,
        "returncode": res.get("returncode"),
        "stdout": res.get("stdout"),
        "stderr": res.get("stderr"),
    }


def build_tools() -> list[Tool]:
    return [
        Tool("deploy_plan", "Detect how a site under /opt would be deployed (SAFE, read-only): shows method and command without running.", DeployParams, deploy_plan, Safety.SAFE),
        Tool("deploy_run", "Deploy a site under /opt on the host via nsenter (DESTRUCTIVE). Requires user confirmation.", DeployParams, deploy_run, Safety.DANGEROUS),
    ]
