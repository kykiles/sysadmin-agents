import uuid
from enum import Enum
from pydantic import BaseModel, Field


class Decision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


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
