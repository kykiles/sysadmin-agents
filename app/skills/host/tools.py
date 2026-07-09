from app.tools.base import Tool, Safety
from app.tools.docker import shell_exec, ShellParams


def build_tools() -> list[Tool]:
    return [
        Tool("shell_exec", "Run a shell command on the host system (DESTRUCTIVE). Requires user confirmation.", ShellParams, shell_exec, Safety.DANGEROUS),
    ]
