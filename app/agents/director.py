import asyncio
from functools import reduce
from operator import or_

from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.loader import compose_prompt
from app.agents.messages import Task, Result
from app.bot.reports import save_report
from app.config import settings
from app.llm.client import LLMClient
from app.logging import get_logger
from app.memory.facts import get_store
from app.skills.memory.tools import build_tools as memory_tools
from app.skills.readonly import HostAccess, build_host_tools
from app.tools.base import Tool, Safety

log = get_logger("director")


class SpawnParams(BaseModel):
    role: str = Field(description="one-line role for the temporary agent, in Russian")
    skills: list[str] = Field(description="names of skills to grant, from the available skills list")
    task: str = Field(description="clear, self-contained task description for the agent")


class MakeReportParams(BaseModel):
    title: str = Field(description="short report title in Russian, used as the file name")
    markdown: str = Field(description="full report body in markdown; tables and headings are fine here")


class RecallExperienceParams(BaseModel):
    query: str = Field(
        description="what the current task is about, in Russian: a few keywords, not a sentence"
    )


_EXPERIENCE_BLOCK = (
    "\n\nОпыт прошлых задач: если задача похожа на уже сделанное, начни с "
    "recall_experience — вернутся прошлые формулировки, итог одной фразой, какими "
    "навыками решали и получилось ли. Неудачный прошлый заход — повод не повторять его.\n\n"
)


def build_director_prompt(available_skills: dict[str, str] | None = None,
                          with_experience: bool = False) -> str:
    spawn_block = ""
    if available_skills:
        skills = "\n".join(f"- {name}: {desc}" for name, desc in available_skills.items())
        spawn_block = (
            "\n\nЗаранее заданных специалистов нет — под каждую задачу ты собираешь временных "
            "агентов через spawn: укажи роль одной фразой, набор навыков и задачу. Агент живёт "
            "одну задачу, его контекст стирается после ответа. Несколько spawn в одном ответе "
            "выполняются параллельно. Последовательность «A и B параллельно → C сводит их "
            "результаты» строится тобой: два spawn параллельно, третьим spawn передай их выводы "
            "в task — межагентного обмена сообщениями нет, шина — это ты.\n"
            f"Доступные навыки:\n{skills}"
        )
    return (
        "Ты — Директор. Получаешь задачи от пользователя через Telegram: любые — от "
        "обслуживания серверов до поиска, разбора документов и подготовки отчётов. "
        "Твоя роль: понять задачу, при необходимости разбить её и поручить "
        "выполнение временным агентам, которых ты собираешь под задачу через spawn. Своих "
        "рабочих инструментов у тебя нет — всю работу делают спавнутые агенты. "
        "Получив результат от агента, сформулируй понятный итоговый отчёт для пользователя на русском. "
        "Если задача тривиальная и не требует агента — ответь сразу.\n\n"
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
        "Память команды: в промпте есть только оглавление — области знаний и число фактов "
        "в каждой. Сами факты не вычитывай целиком: если область относится к задаче, "
        "вызови recall_facts(scope=...). Итог задачи, который пригодится в будущем "
        "(решение, топология, путь, договорённость) — сохрани через remember_fact.\n\n"
        f"{_EXPERIENCE_BLOCK if with_experience else ''}"
        f"{spawn_block}"
    )


# Память — зона ответственности только директора: эти навыки не раздаём спавнутым
# агентам. Свои инструменты памяти директор берёт из memory_tools() напрямую, не из
# библиотеки, поэтому фильтрация библиотеки его не затрагивает.
_DIRECTOR_ONLY = {"memory"}


def _spawnable(skills: dict) -> dict:
    return {n: s for n, s in skills.items() if n not in _DIRECTOR_ONLY}


def _memory_index() -> str:
    """Оглавление памяти в промпт — только области и объём, сами факты по запросу."""
    try:
        scopes = get_store().scopes()
    except RuntimeError:
        return ""
    if not scopes:
        return "\n\nПамять команды пуста."
    lines = "\n".join(f"- {s['scope']}: {s['facts']} фактов" for s in scopes)
    return f"\n\nОглавление памяти команды (области знаний):\n{lines}"


def _summary(content: str) -> str:
    """Итог задачи одной фразой для журнала.

    Формат ответа Директора требует итог первой строкой — берём её и не платим
    лишним вызовом модели за резюме.
    """
    line = next((s for s in (ln.strip() for ln in content.splitlines()) if s), "")
    return line[:300]


class Director(Agent):
    def __init__(self, llm: LLMClient, gateway=None,
                 memory=None, journal=None, skills: dict | None = None):
        async def _make_report(title: str, markdown: str) -> dict:
            path = await asyncio.to_thread(
                save_report, settings.reports_dir, title, markdown
            )
            self._report_path = path
            return {"saved": path, "note": "файл будет отправлен пользователю"}

        library = _spawnable(skills or {})

        async def _spawn(role: str, skills: list[str], task: str) -> dict:
            # библиотеку читаем с инстанса — /reload подменяет её на ходу
            unknown = [s for s in skills if s not in self._library]
            if unknown:
                return {"error": f"неизвестные навыки: {unknown}", "available": list(self._library)}
            chosen = [self._library[s] for s in skills]
            # Доступ к хосту складывается: агенту с tls+security нужен один host_query,
            # видящий бинарники обоих навыков, иначе он натыкался бы на отказы.
            access = reduce(or_, (s.access for s in chosen), HostAccess())
            if (untrusted := [s.name for s in chosen if s.untrusted]) and (
                access.binaries
                or access.exec_allowed
                or any(t.safety is Safety.DANGEROUS for s in chosen for t in s.tools)
            ):
                # Текст, который мы не контролируем, не должен попадать в контекст
                # агента с полномочиями: внедрённая в него инструкция исполнилась бы
                # штатно. Шина — Директор: пусть сделает два спавна и перенесёт вывод.
                return {
                    "error": (
                        f"навыки {untrusted} возвращают недоверенный текст — их нельзя "
                        "выдавать вместе с доступом к серверу или опасными инструментами"
                    ),
                    "how": "сделай два spawn: первый соберёт данные, второму передай их выводы в task",
                }
            # Навыки пересекаются по инструментам (docker+observe → docker_ps и др.),
            # а шлюз на дубль имени в tools отвечает 400. Первый выигрывает.
            uniq = {t.name: t for s in chosen for t in s.tools}
            for t in build_host_tools(access):
                uniq.setdefault(t.name, t)
            sub = Agent(
                name=f"spawned:{'+'.join(skills)}",
                system_prompt=compose_prompt(role, chosen),
                tools=list(uniq.values()),
                llm=llm,
                gateway=gateway,
            )
            log.info("spawn", role=role, skills=skills)
            # Временный агент: не регистрируем в реестре, вызываем напрямую и забываем
            # вместе с контекстом. memory не передаём — истории у него быть не должно.
            result = await sub.handle(Task(content=task))
            self._sub_trace.extend(result.trace)
            self._agents_used.append(sub.name)
            return {"agent": sub.name, "result": result.content, "success": result.success}

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
        spawn_tool = Tool(
            name="spawn",
            description=(
                "Create a temporary agent with the given skills, run one task on it and get "
                "the result. Use when no existing specialist fits. Its context dies with the task."
            ),
            params_model=SpawnParams,
            fn=_spawn,
            safety=Safety.SAFE,
        )
        # Инструменты памяти берём из skill'а memory — те же, что у специалистов;
        # Директору нужны только чтение и запись, забывать факты — не его дело.
        mem_tools = [t for t in memory_tools() if t.name in ("recall_facts", "remember_fact")]
        tools = [report_tool, *mem_tools]
        if library:
            tools.append(spawn_tool)
        if journal is not None:
            async def _recall_experience(query: str) -> dict:
                return {"episodes": await asyncio.to_thread(journal.search, query)}

            tools.append(Tool(
                name="recall_experience",
                description=(
                    "Recall how similar tasks were solved before: past intents, one-line "
                    "outcomes, skills used and whether they succeeded. Safe."
                ),
                params_model=RecallExperienceParams,
                fn=_recall_experience,
                safety=Safety.SAFE,
            ))
        super().__init__(
            name="director",
            system_prompt=build_director_prompt(
                {n: s.description for n, s in library.items()},
                with_experience=journal is not None,
            ),
            tools=tools,
            llm=llm,
            gateway=gateway,
            memory=memory,
        )
        self._library = library
        self._base_prompt = self.system_prompt
        self._journal = journal
        # Задачи Директора идут строго по одной: накопители ниже живут на инстансе,
        # параллельный handle их бы перемешал. Спавнутых агентов замок не касается —
        # они выполняются внутри одной задачи и параллелятся намеренно.
        self._lock = asyncio.Lock()
        self._sub_trace: list[str] = []
        self._agents_used: list[str] = []
        self._report_path: str = ""

    def reload_library(self, skills: dict) -> None:
        """Подхватить обновлённые навыки без рестарта процесса."""
        self._library = _spawnable(skills)
        self._base_prompt = build_director_prompt(
            {n: s.description for n, s in self._library.items()},
            with_experience=self._journal is not None,
        )

    async def handle(self, task: Task) -> Result:
        async with self._lock:
            self._sub_trace = []
            self._agents_used = []
            self._report_path = ""
            self.system_prompt = self._base_prompt + await asyncio.to_thread(_memory_index)
            try:
                result = await super().handle(task)
            finally:
                # «Не спрашивать снова» действует в пределах одной задачи
                if self._gateway is not None:
                    self._gateway.release(task.id)
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
                summary=_summary(result.content),
            )
        except Exception:
            log.exception("journal_write_failed", task_id=task.id)
