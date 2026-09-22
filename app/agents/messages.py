import uuid
from enum import Enum
from pydantic import BaseModel, Field

from app.llm.client import Usage


class Decision(str, Enum):
    APPROVED = "approved"
    # «Yes to all»: этот вызов и дальше такие же (см. ConfirmationRequest.scope).
    APPROVED_ALL = "approved-all"
    # Исполнено без вопроса по ранее данному «Yes to all».
    AUTO_APPROVED = "auto-approved"
    REJECTED = "rejected"

    @property
    def approved(self) -> bool:
        return self is not Decision.REJECTED


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    chat_id: str = ""
    # Корневая задача Директора. У спавнутого агента своя Task, но run — общий:
    # по нему подтверждения и журнал связываются с исходным запросом пользователя.
    run_id: str = ""


class Result(BaseModel):
    task_id: str
    content: str
    success: bool = True
    trace: list[str] = Field(default_factory=list)
    iterations: int = 0
    attachment: str = ""
    # Последний ход модели без вызовов инструментов — собственно ответ. В content
    # он склеен с попутными репликами («сейчас посмотрю»), и итог задачи надо брать
    # отсюда, иначе в журнал уезжает преамбула вместо вывода.
    final: str = ""
    # Полный ход задачи: то, что реально видела модель. Пишет в журнал только
    # Директор — транскрипты спавнутых агентов умирают вместе с ними.
    transcript: list[dict] = Field(default_factory=list)
    # Чего стоила работа этого агента: сумма по всем его ходам.
    usage: Usage = Field(default_factory=Usage)


# Аргументы, которые называют цель вызова: сервер, контейнер, проект, сайт, скрипт.
_SCOPE_KEYS = ("host", "container", "project", "site", "skill", "script")
_OPAQUE_PROGRAMS = {
    "sh", "bash", "dash", "ash", "zsh", "env", "sudo", "su", "nsenter", "busybox",
    "python", "python3", "perl", "node", "xargs", "timeout", "nohup", "ssh",
}
# docker — не одна программа: `restart` и `exec` разрешать вместе нельзя. Эти
# подкоманды исполняют что угодно (внутри контейнера или с томом `/:/host`).
_DOCKER_GROUPS = {"compose", "container"}
_DOCKER_OPAQUE = {"exec", "run", "create"}


def _docker_program(args: list) -> str | None:
    """`docker restart` / `docker compose up`; None — подкоманда исполняет что угодно
    или скрыта за опциями (`docker --context x exec`): её не разбираем, кнопки нет."""
    words = ["docker"]
    for arg in args:
        if not isinstance(arg, str) or arg.startswith("-"):
            return None
        words.append(arg)
        if arg not in _DOCKER_GROUPS:
            return None if arg in _DOCKER_OPAQUE else " ".join(words)
    return None


class ConfirmationRequest(BaseModel):
    """Запрос на один конкретный вызов. Идентификаторы задаёт код, а не модель;
    одноразовый request_id выдаёт шлюз при отправке."""

    run_id: str
    agent_id: str
    tool_call_id: str
    tool_name: str
    # Валидированный снимок аргументов — ровно он показан и ровно он будет исполнен.
    args: dict
    # Пояснение модели для человека; цель и параметры показываются отдельно из args.
    reason: str = ""

    def scope(self) -> str | None:
        """Что разрешает «Yes to all»: инструмент, цель и программа — как префикс
        команды в Claude Code. None — узнаваемой цели нет (MCP, write_skill, сырой
        HTTP), и одним нажатием разрешилось бы что угодно: кнопку не предлагаем."""
        parts = [f"{k}={self.args[k]}" for k in _SCOPE_KEYS if isinstance(self.args.get(k), str)]
        command = self.args.get("command")
        if isinstance(command, list) and command and isinstance(command[0], str):
            # По имени оболочки не видно, что она исполнит: разрешить `sh` навсегда —
            # разрешить любой скрипт.
            name = command[0].rsplit("/", 1)[-1]
            if name in _OPAQUE_PROGRAMS:
                return None
            program = _docker_program(command[1:]) if name == "docker" else command[0]
            if program is None:
                return None
            parts.append(f"program={program}")
        return f"{self.tool_name}: {', '.join(parts)}" if parts else None
