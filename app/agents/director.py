import asyncio

from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.messages import Task, Result
from app.agents.registry import AgentRegistry
from app.bot.reports import save_report
from app.config import settings
from app.llm.client import LLMClient
from app.logging import get_logger
from app.tools.base import Tool, Safety

log = get_logger("director")


class DelegateToParams(BaseModel):
    agent_name: str = Field(description="name of the specialist agent to delegate to")
    task: str = Field(description="clear, self-contained task description for the specialist")


class MakeReportParams(BaseModel):
    title: str = Field(description="short report title in Russian, used as the file name")
    markdown: str = Field(description="full report body in markdown; tables and headings are fine here")


def build_director_prompt(available_agents: dict[str, str]) -> str:
    agents = "\n".join(f"- {name}: {desc}" for name, desc in available_agents.items())
    return (
        "Ты — Директор команды системных администраторов. Получаешь задачи от пользователя "
        "через Telegram. Твоя роль: понять задачу, при необходимости разбить её и делегировать "
        "подходящему специалисту через инструмент delegate_to. У тебя НЕТ прямого доступа к Docker. "
        "Получив результат от специалиста, сформулируй понятный итоговый отчёт для пользователя на русском. "
        "Если задача тривиальная и не требует специалиста — ответь сразу.\n\n"
        "Формат ответа:\n"
        "- Первая строка — итог одной фразой. Детали ниже.\n"
        "- Детали оформляй блоком цитаты: каждая строка начинается с «> ». "
        "Каждая запись — со своей строки, парами «ключ: значение».\n"
        "- Технические значения (IP, порты, пути, имена контейнеров, команды) — "
        "в `обратных кавычках`.\n"
        "- Заголовок раздела — **жирным**.\n"
        "- НЕ используй таблицы: в Telegram их нет, они разъезжаются. "
        "Вместо таблицы — блок цитаты со строками «ключ: значение».\n"
        "- Тон деловой, без лишних слов. Эмодзи не используй.\n\n"
        "Если пользователь просит оформить отчёт (документом, файлом, в .md) — "
        "вызови make_report: туда пиши развёрнутый markdown (в файле таблицы и заголовки "
        "уместны, он читается вне Telegram), а в ответ дай короткий итог на 2-3 строки.\n\n"
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

        async def _make_report(title: str, markdown: str) -> dict:
            path = await asyncio.to_thread(
                save_report, settings.reports_dir, title, markdown
            )
            self._report_path = path
            return {"saved": path, "note": "файл будет отправлен пользователю"}

        delegate_tool = Tool(
            name="delegate_to",
            description="Delegate a task to a specialist agent and receive its result",
            params_model=DelegateToParams,
            fn=_delegate,
            safety=Safety.SAFE,
        )
        report_tool = Tool(
            name="make_report",
            description=(
                "Save a report as a .md file and send it to the user as a document. "
                "Use when the user asks for a report/file/document."
            ),
            params_model=MakeReportParams,
            fn=_make_report,
            safety=Safety.SAFE,
        )
        super().__init__(
            name="director",
            system_prompt=build_director_prompt(available_agents),
            tools=[delegate_tool, report_tool],
            llm=llm,
            registry=registry,
            memory=memory,
        )
        self._journal = journal
        self._sub_trace: list[str] = []
        self._agents_used: list[str] = []
        self._report_path: str = ""

    async def handle(self, task: Task) -> Result:
        # Реестр отдаёт агенту задачи по одной (_consume), поэтому накопители на
        # инстансе безопасны — параллельных handle у Директора не бывает.
        self._sub_trace = []
        self._agents_used = []
        self._report_path = ""
        result = await super().handle(task)
        result.attachment = self._report_path
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
