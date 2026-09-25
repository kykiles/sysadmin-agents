import uuid
from enum import Enum
from pydantic import BaseModel, Field

from app.llm.client import Usage
from app.skills.readonly import KNOWN_BINARIES, is_read_only


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
# У команды «Да, для всех таких» разрешает программу вместе с подкомандой, и только
# у программ, где подкоманда сама говорит, что будет сделано. У остальных смысл
# задают аргументы, и разрешение на программу было разрешением на что угодно:
# одобренный «для всех» `rm /tmp/old.log` пропускал `rm -rf /var/lib/postgresql`,
# `systemctl reload nginx` — `systemctl disable --now ssh`, `curl -sI …` —
# `curl -d @.env …`, `psql -c "select 1"` — любой SQL. Оболочки, `iptables`,
# клиенты БД — всё не из списка — подтверждаются каждый раз.
_VERB_PROGRAMS = {"systemctl", "certbot"}
# docker — не одна программа: `restart` и `exec` разрешать вместе нельзя. В группах
# (`docker volume rm`, `docker system prune`) подкоманда — следующее слово: иначе
# `docker volume ls` разрешал бы `docker volume rm`. Опасные подкоманды исполняют
# что угодно (внутри контейнера или с томом `/:/host`) или пишут и отправляют куда
# угодно (`cp` в файлы хоста, `push` в чужой реестр).
_DOCKER_GROUPS = {
    "builder", "buildx", "compose", "config", "container", "context", "image", "manifest",
    "network", "node", "plugin", "secret", "service", "stack", "swarm", "system", "trust",
    "volume",
}
_DOCKER_OPAQUE = {"exec", "run", "create", "cp", "push"}


def _docker_program(args: list) -> str | None:
    """`docker restart` / `docker compose up`; None — подкоманда исполняет что угодно
    или скрыта за опциями (`docker --context x exec`): её не разбираем, кнопки нет."""
    words = ["docker"]
    for arg in args:
        if arg.startswith("-"):
            return None
        words.append(arg)
        if arg not in _DOCKER_GROUPS:
            return None if arg in _DOCKER_OPAQUE else " ".join(words)
    return None


def _command_scope(command: list[str]) -> str | None:
    """Часть scope про команду; None — кнопки нет."""
    # Путь вместо имени — чужой бинарник под знакомым именем: `/tmp/x/docker restart`.
    if "/" in command[0]:
        return None
    name, args = command[0], command[1:]
    if name == "docker":
        program = _docker_program(args)
    elif name in _VERB_PROGRAMS and args and not any(a.startswith("-") for a in args):
        # Без опций: `certbot renew --post-hook …` исполняет что угодно, такой вызов
        # человек подтверждает отдельно.
        program = f"{name} {args[0]}"
    else:
        program = None
    if program is not None:
        return f"program={program}"
    # Читающая команда — по тому же классификатору, что у host_query: разрешение
    # дойдёт только до вызовов, которые он тоже признает читающими.
    if is_read_only(command, KNOWN_BINARIES):
        return f"program={name}, только чтение"
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
        """Что разрешает «Yes to all»: инструмент, цель и программа с подкомандой — как
        префикс команды в Claude Code. None — кнопку не предлагаем: узнаваемой цели
        нет (MCP, write_skill, сырой HTTP) или смысл команды задают её аргументы, и
        одним нажатием разрешилось бы что угодно."""
        parts = [f"{k}={self.args[k]}" for k in _SCOPE_KEYS if isinstance(self.args.get(k), str)]
        command = self.args.get("command")
        if isinstance(command, list) and command:
            if not all(isinstance(a, str) for a in command):
                return None
            program = _command_scope(command)
            if program is None:
                return None
            parts.append(program)
        return f"{self.tool_name}: {', '.join(parts)}" if parts else None
