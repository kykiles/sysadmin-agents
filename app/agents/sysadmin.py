from app.agents.base import Agent
from app.agents.registry import AgentRegistry
from app.llm.client import LLMClient
from app.tools.docker import build_sysadmin_tools

SYSADMIN_PROMPT = (
    "Ты — Главный системный администратор. Ты управляешь Docker-контейнерами и compose-проектами "
    "на текущем сервере. Получаешь технические подзадачи от Директора. Используй инструменты для "
    "сбора данных (логи, статусы, инспекция) и анализа. Опасные операции (перезапуск, остановка, "
    "exec, compose up/down) требуют подтверждения пользователя — система спросит его автоматически, "
    "просто вызывай нужный инструмент. Возвращай структурированный, технически точный отчёт на русском. "
    "Не выдумывай данные — только то, что вернули инструменты."
)


class ChiefSysadmin(Agent):
    def __init__(self, llm: LLMClient, registry: AgentRegistry):
        super().__init__(
            name="sysadmin",
            system_prompt=SYSADMIN_PROMPT,
            tools=build_sysadmin_tools(),
            llm=llm,
            registry=registry,
        )
