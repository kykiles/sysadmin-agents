from app.tools.base import Tool, Safety
from app.tools.docker import (
    docker_ps, docker_logs, docker_stats, docker_inspect,
    docker_restart, docker_stop, docker_start, docker_exec,
    compose_ls, compose_ps, compose_up, compose_down,
    NoParams, LogsParams, ContainerParams, ExecParams, ProjectParams,
)


def build_tools() -> list[Tool]:
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
        Tool("docker_exec", "Run any command inside a container (DESTRUCTIVE — may modify data). Requires user confirmation.", ExecParams, docker_exec, Safety.DANGEROUS),
    ]
