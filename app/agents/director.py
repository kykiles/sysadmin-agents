import asyncio
import re
from dataclasses import replace
from functools import reduce
from operator import or_
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.loader import compose_prompt
from app.agents.messages import Task, Result
from app.bot.reports import save_report
from app.config import settings
from app.llm.client import LLMClient
from app.logging import get_logger
from app.memory.facts import get_store
from app.skills.loader import load_all_skills
from app.memory.tools import build_tools as memory_tools
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


class WriteSkillParams(BaseModel):
    name: str = Field(description="snake_case skill name, latin, e.g. weekly_report")
    description: str = Field(
        description="one line saying WHEN it applies and WHAT it does — this is the only "
                    "thing you will see when choosing skills later, so name the triggers: "
                    "'when asked for a weekly traffic report: collects ... and formats ...'"
    )
    instructions: str = Field(
        description="the playbook itself, in Russian markdown: steps in order, which skills "
                    "to grant the agent, what to check, known pitfalls. Explain why a step "
                    "matters instead of writing ВСЕГДА/НИКОГДА — reasons generalise, rules don't"
    )


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,30}$")

# Плейбук уходит в промпт агента при каждом spawn, поэтому длина — это цена в токенах
# на всю его дальнейшую жизнь. Ручные скилы укладываются в 60 строк; просьбу в промпте
# модель проигнорирует, предел механический.
_SKILL_MAX_CHARS = 6000

_WRITE_SKILL_BLOCK = (
    "\n\nПроцедурная память: если задача решена нетривиальной последовательностью, "
    "которая повторится (порядок шагов, какие навыки выдавать агенту, что проверять, "
    "где грабли) — сохрани её плейбуком через write_skill. Он попадёт в список навыков "
    "и будет доступен при следующем spawn. Так система запоминает не факт, "
    "а способ работы. Новых инструментов плейбук не создаёт — он опирается на "
    "существующие навыки, их и перечисли в инструкциях. Пиши коротко и по делу: "
    "плейбук уходит в промпт агента целиком.\n"
)


_EXPERIENCE_BLOCK = (
    "\n\nОпыт прошлых задач: если задача похожа на уже сделанное, начни с "
    "recall_experience — вернутся прошлые формулировки, итог одной фразой и какими "
    "навыками решали.\n\n"
)


def build_director_prompt(available_skills: dict[str, str] | None = None,
                          with_experience: bool = False,
                          with_write_skill: bool = False) -> str:
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
        "Память команды: в промпте есть оглавление — области знаний и ключи фактов. "
        "Значения не вычитывай целиком: если ключ относится к задаче, вызови "
        "recall_facts(scope=...) и работай от него — заново собирать то, что уже "
        "записано, значит платить за это второй раз.\n"
        "Что сохранять через remember_fact: только то, что переживёт эту задачу и "
        "понадобится в следующей — топология, пути, версии, принятые решения, "
        "договорённости. Разовые находки («что было в этих логах», «почему упал этот "
        "запрос») в память не клади: они уходят пользователю ответом или отчётом, "
        "а в памяти станут мусором, который придётся вычищать.\n"
        "Пиши под тем же ключом, который уже есть в оглавлении: запись под новым "
        "именем не заменяет старый факт, а плодит второй такой же, и дальше непонятно, "
        "какой из них верен.\n"
        "Область (scope) — это тема, а не хост. Бери её из оглавления выше; заводи "
        "новую, только если ни одна не подходит. Если в области больше тридцати фактов, "
        "она слишком широкая — раздели по темам, иначе оглавление перестаёт экономить "
        "токены: recall вернёт почти всю память.\n\n"
        f"{_EXPERIENCE_BLOCK if with_experience else ''}"
        f"{_WRITE_SKILL_BLOCK if with_write_skill else ''}"
        f"{spawn_block}"
    )


# Сколько раз агент может дёрнуть инструменты недоверенного скила за одну задачу.
# Просьба в плейбуке («два поиска и одно извлечение») слабой моделью игнорируется —
# живой поиск делал по восемь запросов и пять извлечений, и время уходило не в сеть,
# а в генерации поверх раздутого контекста. Здесь предел механический.
# ponytail: одна константа на все такие скилы; в настройки, если понадобится крутить
# без пересборки
_UNTRUSTED_CALL_BUDGET = 6


def _budgeted(tools: list[Tool], limit: int) -> list[Tool]:
    """Обернуть инструменты общим счётчиком вызовов. Счётчик живёт в замыкании,
    поэтому у каждого спавна он свой."""
    left = limit

    def wrap(tool: Tool) -> Tool:
        async def fn(**kwargs):
            nonlocal left
            if left <= 0:
                return {"error": (
                    f"бюджет вызовов исчерпан ({limit}) — отвечай тем, что уже собрал"
                )}
            left -= 1
            return await tool.fn(**kwargs)

        return replace(tool, fn=fn)

    return [wrap(t) for t in tools]


def _memory_index() -> str:
    """Оглавление памяти в промпт — области и ключи фактов, сами значения по запросу.

    Одного счётчика фактов не хватало: по «bot: 11 фактов» модель не видела, что
    ответ уже лежит в памяти, и шла добывать его заново, а потом записывала под
    новым ключом (топология БД осела четырьмя фактами). Ключи стоят единицы
    токенов на факт и снимают оба промаха.
    """
    try:
        keys = get_store().keys()
    except RuntimeError:
        return ""
    if not keys:
        return "\n\nПамять команды пуста."
    lines = "\n".join(f"- {s['scope']}: {', '.join(s['keys'])}" for s in keys)
    return f"\n\nОглавление памяти команды (области знаний и ключи фактов):\n{lines}"


def _summary(content: str) -> str:
    """Итог задачи одной фразой для журнала.

    Формат ответа Директора требует итог первой строкой — берём её и не платим
    лишним вызовом модели за резюме.
    """
    line = next((s for s in (ln.strip() for ln in content.splitlines()) if s), "")
    return line[:300]


class Director(Agent):
    def __init__(self, llm: LLMClient, gateway=None,
                 memory=None, journal=None, skills: dict | None = None,
                 skills_dir: Path | None = None):
        async def _make_report(title: str, markdown: str) -> dict:
            path = await asyncio.to_thread(
                save_report, settings.reports_dir, title, markdown
            )
            self._report_path = path
            return {"saved": path, "note": "файл будет отправлен пользователю"}

        library = skills or {}

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
            uniq = {
                t.name: t
                for s in chosen
                for t in (_budgeted(s.tools, _UNTRUSTED_CALL_BUDGET) if s.untrusted else s.tools)
            }
            for t in build_host_tools(access):
                uniq.setdefault(t.name, t)
            sub = Agent(
                name=f"spawned:{'+'.join(skills)}",
                system_prompt=compose_prompt(role, chosen),
                tools=list(uniq.values()),
                llm=llm,
                gateway=gateway,
            )
            # Вывод такого агента вернётся в контекст Директора: всё, что он запишет
            # в память по итогам этой задачи, помечаем как пришедшее из чужого текста.
            self._untrusted_used = self._untrusted_used or any(s.untrusted for s in chosen)
            log.info("spawn", role=role, skills=skills)
            # Временный агент: не регистрируем в реестре, вызываем напрямую и забываем
            # вместе с контекстом. memory не передаём — истории у него быть не должно.
            result = await sub.handle(Task(content=task))
            self._sub_trace.extend(result.trace)
            self._agents_used.append(sub.name)
            return {"agent": sub.name, "result": result.content, "success": result.success}

        async def _write_skill(name: str, description: str, instructions: str) -> dict:
            if not _SKILL_NAME.match(name):
                return {"error": "имя навыка: латиница snake_case, 3-31 символ"}
            if len(instructions) > _SKILL_MAX_CHARS:
                return {"error": f"плейбук длиннее {_SKILL_MAX_CHARS} символов — сократи до сути"}
            d = skills_dir / name
            # Граница простая: плейбуки пишет модель, код пишет человек. Скил с
            # tools.py несёт права доступа к хосту, и переписать его инструкции —
            # значит переписать ограничения, под которыми их выдали.
            if (d / "tools.py").exists():
                return {"error": f"навык {name} содержит код — его плейбук правит человек"}
            meta = yaml.safe_dump(
                {"name": name, "description": description},
                allow_unicode=True, sort_keys=False,
            )
            body = f"---\n{meta}---\n\n{instructions.strip()}\n"

            def _save() -> None:
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(body, encoding="utf-8")

            await asyncio.to_thread(_save)
            self.reload_library(await asyncio.to_thread(load_all_skills, skills_dir))
            log.info("write_skill", skill=name)
            return {"saved": name, "note": "навык доступен для spawn сразу"}

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
        # Память принадлежит Директору: инструменты приходят из ядра, а не из
        # библиотеки скилов, поэтому выдать их спавнутому агенту нечем.
        tools = [report_tool, *memory_tools(lambda: self._untrusted_used)]
        if library:
            tools.append(spawn_tool)
        if skills_dir is not None:
            tools.append(Tool(
                name="write_skill",
                description=(
                    "Save a reusable playbook as a skill (SKILL.md) so future tasks can spawn "
                    "an agent with it. Text only — it grants no new tools. Requires confirmation."
                ),
                params_model=WriteSkillParams,
                fn=_write_skill,
                safety=Safety.DANGEROUS,
            ))
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
                with_write_skill=skills_dir is not None,
            ),
            tools=tools,
            llm=llm,
            gateway=gateway,
            memory=memory,
        )
        self._library = library
        self._skills_dir = skills_dir
        self._base_prompt = self.system_prompt
        self._journal = journal
        # Задачи Директора идут строго по одной: накопители ниже живут на инстансе,
        # параллельный handle их бы перемешал. Спавнутых агентов замок не касается —
        # они выполняются внутри одной задачи и параллелятся намеренно.
        self._lock = asyncio.Lock()
        self._untrusted_used = False
        self._sub_trace: list[str] = []
        self._agents_used: list[str] = []
        self._report_path: str = ""

    def reload_library(self, skills: dict) -> None:
        """Подхватить обновлённые навыки без рестарта процесса."""
        self._library = skills
        self._base_prompt = build_director_prompt(
            {n: s.description for n, s in self._library.items()},
            with_experience=self._journal is not None,
            with_write_skill=self._skills_dir is not None,
        )

    async def handle(self, task: Task) -> Result:
        async with self._lock:
            self._sub_trace = []
            self._agents_used = []
            self._untrusted_used = False
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
