import asyncio

from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.messages import Task, Result
from app.agents.registry import AgentRegistry
from app.llm.client import LLMClient
from app.logging import get_logger
from app.tools.base import Tool, Safety

log = get_logger("director")


class DelegateToParams(BaseModel):
    agent_name: str = Field(description="name of the specialist agent to delegate to")
    task: str = Field(description="clear, self-contained task description for the specialist")


def build_director_prompt(available_agents: dict[str, str]) -> str:
    agents = "\n".join(f"- {name}: {desc}" for name, desc in available_agents.items())
    return (
        "Ты — Директор команды системных администраторов. Получаешь задачи от пользователя "
        "через Telegram. Твоя роль: понять задачу, при необходимости разбить её и делегировать "
        "подходящему специалисту через инструмент delegate_to. У тебя НЕТ прямого доступа к Docker. "
        "Получив результат от специалиста, сформулируй понятный итоговый отчёт для пользователя на русском. "
        "Если задача тривиальная и не требует специалиста — ответь сразу.\n\n"
        "Формат ответа (сообщение уходит в Telegram как обычный текст):\n"
        "- НЕ используй markdown-таблицы и вообще markdown-разметку (**жирный**, `код`, |столбцы|) — "
        "она не рендерится и разъезжается. Пиши простым текстом.\n"
        "- Перечни выводи строками-списками через «— »; пары данных — по строке в виде «ключ: значение».\n"
        "- Тон деловой и строгий, без лишних слов. Эмодзи не используй "
        "(допустимо максимум один статусный значок в начале строки, если он реально несёт смысл).\n"
        "- Кратко и по делу: сначала итог, при необходимости — детали ниже.\n\n"
        f"Доступные специалисты:\n{agents}"
    )


class Director(Agent):
    def __init__(self, llm: LLMClient, registry: AgentRegistry, available_agents: dict[str, str],
                 memory=None, journal=None):
        async def _delegate(agent_name: str, task: str) -> dict:
            result = await registry.request(agent_name, Task(content=task))
            self._sub_trace.extend(result.trace)
            self._agents_used.append(agent_name)
            return {"agent": agent_name, "result": result.content, "success": result.success}

        delegate_tool = Tool(
            name="delegate_to",
            description="Delegate a task to a specialist agent and receive its result",
            params_model=DelegateToParams,
            fn=_delegate,
            safety=Safety.SAFE,
        )
        super().__init__(
            name="director",
            system_prompt=build_director_prompt(available_agents),
            tools=[delegate_tool],
            llm=llm,
            registry=registry,
            memory=memory,
        )
        self._journal = journal
        self._sub_trace: list[str] = []
        self._agents_used: list[str] = []

    async def handle(self, task: Task) -> Result:
        # Реестр отдаёт агенту задачи по одной (_consume), поэтому накопители на
        # инстансе безопасны — параллельных handle у Директора не бывает.
        self._sub_trace = []
        self._agents_used = []
        result = await super().handle(task)
        if self._journal is not None:
            await self._write_journal(task, result)
        return result

    async def _write_journal(self, task: Task, result: Result) -> None:
        try:
            await asyncio.to_thread(
                self._journal.record,
                task_id=task.id,
                chat_id=task.chat_id,
                intent=task.content,
                agents=self._agents_used,
                tool_seq=result.trace + self._sub_trace,
                iterations=result.iterations,
                success=result.success,
            )
        except Exception:
            log.exception("journal_write_failed", task_id=task.id)
