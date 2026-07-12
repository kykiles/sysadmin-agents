import asyncio
import os
from typing import Any
from pydantic import BaseModel, Field
from aiodocker import Docker
from app.config import settings


# ---------- params ----------

class ContainerParams(BaseModel):
    container: str = Field(description="container name or id")


class LogsParams(BaseModel):
    container: str
    tail: int = Field(default=200, description="number of trailing log lines")


class ExecParams(BaseModel):
    container: str
    command: list[str] = Field(description="command argv to run inside container")


class ShellParams(BaseModel):
    command: list[str] = Field(description="command argv to run on host shell")


class NoParams(BaseModel):
    pass


class ProjectParams(BaseModel):
    project: str = Field(description="compose project dir name under COMPOSE_PROJECTS_DIR")


# ---------- projections (keep LLM context small) ----------

def _project_container(c: dict) -> dict:
    return {k: c.get(k) for k in ("Id", "Names", "Image", "State", "Status", "Ports") if k in c}


def _project_inspect(info: dict) -> dict:
    state = info.get("State") or {}
    config = info.get("Config") or {}
    host = info.get("HostConfig") or {}
    net = info.get("NetworkSettings") or {}
    return {
        "Id": info.get("Id"),
        "Name": info.get("Name"),
        "Image": config.get("Image"),
        "State": {k: state.get(k) for k in (
            "Status", "Running", "Paused", "Restarting", "OOMKilled",
            "Dead", "ExitCode", "StartedAt", "FinishedAt", "Health") if k in state},
        "RestartCount": info.get("RestartCount"),
        "RestartPolicy": host.get("RestartPolicy"),
        "Ports": net.get("Ports"),
        "Mounts": info.get("Mounts"),
    }


def _project_stats(stats: Any) -> dict:
    if not isinstance(stats, dict):
        return {"raw": stats}
    mem = stats.get("memory_stats") or {}
    cpu = stats.get("cpu_stats") or {}
    return {
        "memory_usage": mem.get("usage"),
        "memory_limit": mem.get("limit"),
        "cpu_total_usage": (cpu.get("cpu_usage") or {}).get("total_usage"),
        "system_cpu_usage": cpu.get("system_cpu_usage"),
        "online_cpus": cpu.get("online_cpus"),
        "pids": (stats.get("pids_stats") or {}).get("current"),
    }


# ---------- safe docker api ----------

async def docker_ps() -> dict:
    async with Docker() as docker:
        containers = await docker.containers.list(all=True)
        return {"containers": [_project_container(c._container) for c in containers]}


async def docker_logs(container: str, tail: int = 200) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        logs = await c.log(stdout=True, stderr=True, tail=tail)
        return {"container": container, "logs": "".join(logs)}


async def docker_stats(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        stats = await c.stats(stream=False)
        return {"container": container, "stats": _project_stats(stats)}


async def docker_inspect(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        info = await c.show()
        return {"container": container, "inspect": _project_inspect(info)}


# ---------- dangerous docker api ----------

async def docker_restart(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        await c.restart()
        return {"restarted": container}


async def docker_stop(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        await c.stop()
        return {"stopped": container}


async def docker_start(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        await c.start()
        return {"started": container}


async def docker_exec(container: str, command: list[str]) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        exec_obj = await c.exec(cmd=command)
        stream = exec_obj.start(detach=False)
        try:
            msg = await asyncio.wait_for(stream.read_out(), timeout=settings.shell_timeout_seconds)
        except asyncio.TimeoutError:
            return {"container": container, "command": command, "output": "",
                    "exit_code": None, "timed_out": True}
        output = msg.data.decode(errors="replace") if msg.data else ""
        inspect = await exec_obj.inspect()
        return {"container": container, "command": command, "output": output, "exit_code": inspect.get("ExitCode")}


# ---------- host shell ----------

async def _run_subprocess(command: list[str], timeout: float | None = None) -> dict:
    to = timeout if timeout is not None else settings.shell_timeout_seconds
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=to)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": f"команда прервана по таймауту ({to}s)",
            "timed_out": True,
        }
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


async def shell_exec(command: list[str]) -> dict:
    return await _run_subprocess(command)


# ---------- host access (via nsenter into pid 1 namespaces) ----------

def _nsenter(argv: list[str]) -> list[str]:
    return ["nsenter", "-t", "1", "-m", "-p", "--", *argv]


async def host_exec(command: list[str]) -> dict:
    """Run an argv command on the real host (mount/pid namespace of pid 1)."""
    return await _run_subprocess(_nsenter(command))


async def host_shell(script: str) -> dict:
    """Run a shell pipeline on the real host via `bash -lc`."""
    return await _run_subprocess(_nsenter(["bash", "-lc", script]))


# ---------- compose (subprocess) ----------

def _project_dir(project: str) -> str:
    return os.path.join(settings.compose_projects_dir, project)


async def _compose(project: str, *args: str) -> dict:
    pd = _project_dir(project)
    cmd = ["docker", "compose", "--project-directory", pd, "-f", os.path.join(pd, "docker-compose.yml"), *args]
    res = await _run_subprocess(cmd)
    return {
        "project": project,
        "command": list(args),
        "returncode": res["returncode"],
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        **({"timed_out": True} if res.get("timed_out") else {}),
    }


async def compose_ls() -> dict:
    root = settings.compose_projects_dir
    projects = []
    for name in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, name, "docker-compose.yml")):
            projects.append(name)
    return {"projects": projects}


async def compose_ps(project: str) -> dict:
    return await _compose(project, "ps", "--format", "json")


async def compose_up(project: str) -> dict:
    return await _compose(project, "up", "-d")


async def compose_down(project: str) -> dict:
    return await _compose(project, "down")
