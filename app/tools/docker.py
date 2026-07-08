import asyncio
import os
from pydantic import BaseModel, Field
from aiodocker import Docker
from app.tools.base import Tool, Safety
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


class NoParams(BaseModel):
    pass


class ProjectParams(BaseModel):
    project: str = Field(description="compose project dir name under COMPOSE_PROJECTS_DIR")


# ---------- safe docker api ----------

async def docker_ps() -> dict:
    async with Docker() as docker:
        containers = await docker.containers.list(all=True)
        return {"containers": [c.attrs for c in containers]}


async def docker_logs(container: str, tail: int = 200) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        logs = await c.log(stdout=True, stderr=True, tail=tail)
        return {"container": container, "logs": "".join(logs)}


async def docker_stats(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        stats = await c.stats(stream=False)
        return {"container": container, "stats": stats}


async def docker_inspect(container: str) -> dict:
    async with Docker() as docker:
        c = docker.containers.container(container)
        info = await c.show()
        return {"container": container, "inspect": info}


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
        output = ""
        async for chunk in stream:
            if isinstance(chunk, bytes):
                output += chunk.decode(errors="replace")
            else:
                output += str(chunk)
        inspect = await exec_obj.inspect()
        return {"container": container, "command": command, "output": output, "exit_code": inspect.get("ExitCode")}


# ---------- compose (subprocess) ----------

def _project_dir(project: str) -> str:
    return os.path.join(settings.compose_projects_dir, project)


async def _compose(project: str, *args: str) -> dict:
    pd = _project_dir(project)
    cmd = ["docker", "compose", "--project-directory", pd, "-f", os.path.join(pd, "docker-compose.yml"), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "project": project,
        "command": list(args),
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
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


# ---------- registry ----------

def build_sysadmin_tools() -> list[Tool]:
    return [
        Tool("docker_ps", "List all docker containers. Use this to see what containers are available before inspecting specific ones.", NoParams, docker_ps, Safety.SAFE),
        Tool("docker_logs", "Get container logs", LogsParams, docker_logs, Safety.SAFE),
        Tool("docker_stats", "Get container resource stats", ContainerParams, docker_stats, Safety.SAFE),
        Tool("docker_inspect", "Inspect container details", ContainerParams, docker_inspect, Safety.SAFE),
        Tool("compose_ls", "List docker compose projects under COMPOSE_PROJECTS_DIR", NoParams, compose_ls, Safety.SAFE),
        Tool("compose_ps", "List services of a compose project", ProjectParams, compose_ps, Safety.SAFE),
        Tool("docker_restart", "Restart a container (DESTRUCTIVE)", ContainerParams, docker_restart, Safety.DANGEROUS),
        Tool("docker_stop", "Stop a container (DESTRUCTIVE)", ContainerParams, docker_stop, Safety.DANGEROUS),
        Tool("docker_start", "Start a container (DESTRUCTIVE)", ContainerParams, docker_start, Safety.DANGEROUS),
        Tool("compose_up", "Run docker compose up -d for a project (DESTRUCTIVE)", ProjectParams, compose_up, Safety.DANGEROUS),
        Tool("compose_down", "Run docker compose down for a project (DESTRUCTIVE)", ProjectParams, compose_down, Safety.DANGEROUS),
        Tool("docker_exec", "Run a command inside a container (DESTRUCTIVE)", ExecParams, docker_exec, Safety.DANGEROUS),
    ]
