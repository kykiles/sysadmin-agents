from app.tools.base import Tool, Safety
from app.tools.docker import docker_exec, ExecParams


def build_tools() -> list[Tool]:
    return [
        Tool("docker_query", "Run a read-only query inside a container (psql, mysql, sqlite3, cat logs, etc.). Safe, auto-executed.", ExecParams, docker_exec, Safety.SAFE),
    ]
