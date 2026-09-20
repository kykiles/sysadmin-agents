import asyncio
import json
import re
import time
from dataclasses import replace
from functools import reduce
from operator import or_
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from app.agents.base import Agent
from app.agents.loader import compose_prompt
from app.agents.episode import Episode
from app.agents.messages import Task, Result
from app.bot.reports import save_report
from app.config import settings
from app.llm.client import LLMClient, Usage
from agent_memory.facts import KIND_LABELS, KnowledgeStore
from app.logging import get_logger, redact
from app.skills.loader import load_all_skills
from app.memory.tools import build_tools as memory_tools
from app.skills.readonly import HostAccess, build_host_tools
from app.skills.resources import build_resource_tools
from app.tools.base import Tool, Safety

log = get_logger("director")


class SpawnParams(BaseModel):
    role: str = Field(description="one-line role for the temporary agent, in Russian")
    skills: list[str] = Field(description="names of skills to grant, from the available skills list")
    task: str = Field(description="clear, self-contained task description for the agent")
    steps: list[int] = Field(default_factory=list,
                             description="numbers of the plan steps (from 1) this agent performs; "
                                         "required when there is a plan")


class MarkStepParams(BaseModel):
    step: int = Field(description="номер пункта плана из твоего списка")
    status: Literal["done", "failed", "skipped"] = Field(
        description="done — выполнен, failed — не удался, skipped — не понадобился")


class PlanParams(BaseModel):
    title: str = Field(description="task title in Russian, a few words")
    steps: list[str] = Field(description="2-6 steps in plain Russian a non-technical user understands")


class MakeReportParams(BaseModel):
    title: str = Field(description="short report title in Russian, used as the file name")
    markdown: str = Field(description="full report body in markdown; tables and headings are fine here")


class RecallExperienceParams(BaseModel):
    query: str = Field(
        description="what the current task is about, in Russian: a few keywords, not a sentence"
    )


class WriteSkillParams(BaseModel):
    name: str = Field(description="kebab-case skill name, latin, e.g. weekly-report")
    description: str = Field(
        description="one line saying WHEN it applies and WHAT it does — this is the only "
                    "thing you will see when choosing skills later, so name the triggers, "
                    "including indirect ones: how the user would put it without naming the "
                    "task. 'when asked for a weekly traffic report, or how much users "
                    "downloaded, or whether traffic grew: collects ... and formats ...'"
    )
    instructions: str = Field(
        description="the playbook itself, in Russian markdown: steps in order, which skills "
                    "to grant the agent, what to check, known pitfalls. Explain why a step "
                    "matters instead of writing ВСЕГДА/НИКОГДА — reasons generalise, rules don't"
    )
    overwrite: bool = Field(
        default=False,
        description="true only to replace an existing skill after reading its current "
                    "playbook: merge the old steps and pitfalls into the new text",
    )


# Формат Agent Skills: строчные латинские буквы, цифры, дефисы; имя = каталог.
_SKILL_NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")

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
    "recall_experience — вернутся прошлые формулировки, итог одной фразой, какими "
    "навыками решали и что тогда не вышло: на что пользователь не дал добро, какие "
    "инструменты падали, какие пункты плана остались несделанными. Это подсказка, "
    "а не запрет: похожая задача могла упереться в то же самое.\n\n"
)


_PLAN_BLOCK = (
    "План: прежде чем поручать работу агентам, вызови plan — заголовок и 2–6 пунктов "
    "обычным языком, понятным человеку без знания команд. Пользователь видит этот список "
    "в Telegram и следит по нему за ходом задачи. В каждом spawn указывай steps — номера "
    "всех пунктов, которые выполняет агент (один агент может вести несколько): он сам "
    "отметит их выполнение. Пункты плана — только работа агентов: свести результаты и "
    "ответить пользователю ты делаешь сам, это не пункт. Изменился план по ходу — вызови "
    "plan снова. Задаче, где агенты не нужны, план не нужен.\n\n"
)


def build_director_prompt(available_skills: dict[str, str] | None = None,
                          with_experience: bool = False,
                          with_write_skill: bool = False,
                          with_plan: bool = False) -> str:
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
            "Памяти у агента нет: всю известную тебе конкретику (имена контейнеров, хостов, "
            "пути, учётки, схемы) выпиши в task дословно. Не переданное он будет угадывать "
            "перебором.\n"
            "Опасные действия (перезапуск, пересборка, изменения) система сама подтверждает "
            "у пользователя кнопками в момент вызова. Поэтому не проси «подтвердите» текстом "
            "и не поручай агенту сначала запросить подтверждение: просят сделать — поручай "
            "сделать. Если агент сообщил, что пользователь отказал, не поручай то же действие "
            "снова.\n"
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
        "Память команды: в промпте есть оглавление — области знаний, ключи фактов и "
        "когда они пригодятся; самих значений там нет.\n"
        "Припоминай до работы: если хоть один ключ относится к задаче, первым ходом "
        "вызови recall_facts(scope=...) — до plan и до spawn. План, составленный без "
        "фактов, отправляет агента добывать заново то, что уже записано, а конкретику "
        "из фактов (имена, пути, селекторы) нужно выписать агенту в task: своей памяти "
        "у него нет.\n"
        "Что сохранять через remember_fact: только то, что переживёт эту задачу и "
        "понадобится в следующей — топология, пути, версии, принятые решения, "
        "договорённости. Разовые находки («что было в этих логах», «почему упал этот "
        "запрос») в память не клади: они уходят пользователю ответом или отчётом, "
        "а в памяти станут мусором, который придётся вычищать.\n"
        "Пиши под тем же ключом, который уже есть в оглавлении: запись под новым "
        "именем не заменяет старый факт, а плодит второй такой же, и дальше непонятно, "
        "какой из них верен. Если remember_fact вернул similar — это и есть тот случай: "
        "разберись, про то же самое речь или нет.\n"
        "К каждому факту давай description — одной фразой, когда он пригодится. "
        "Именно по описанию факт находится из задачи, сформулированной не теми словами, "
        "что ключ; в оглавлении ты видишь описания рядом с ключами.\n"
        "Область (scope) — это тема, а не хост. Бери её из оглавления выше; заводи "
        "новую, только если ни одна не подходит. Если в области больше тридцати фактов, "
        "она слишком широкая — раздели по темам, иначе оглавление перестаёт экономить "
        "токены: recall вернёт почти всю память.\n\n"
        f"{_EXPERIENCE_BLOCK if with_experience else ''}"
        f"{_WRITE_SKILL_BLOCK if with_write_skill else ''}"
        f"{_PLAN_BLOCK if with_plan and available_skills else ''}"
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


def _identity(tool: Tool) -> tuple:
    """Что считается одним инструментом: тот же удалённый метод того же сервера
    или тот же код с той же моделью параметров и тем же уровнем риска."""
    if tool.remote is not None:
        return (tool.remote, tool.safety)
    return (tool.fn, tool.params_model, tool.safety)


def _steps_block(steps: dict[int, str]) -> str:
    lines = "\n".join(f"{n}. {text}" for n, text in steps.items())
    return (
        "Пункты плана, за которые отвечаешь ты (пользователь следит за ними списком "
        f"в Telegram):\n{lines}\n"
        "Как только пункт выполнен, отметь его mark_step(step, \"done\") — можно в том же "
        "ходе, что и другие вызовы. Не удался — \"failed\", не понадобился — \"skipped\". "
        "Отмечай по порядку и только сделанное: если пользователь отказал в подтверждении, "
        "пункт не выполнен."
    )


def _memory_index(facts: KnowledgeStore | None) -> str:
    """Оглавление памяти в промпт — области, ключи и «когда пригодится»; сами
    значения по запросу.

    Одного счётчика фактов не хватало: по «bot: 11 фактов» модель не видела, что
    ответ уже лежит в памяти, и шла добывать его заново, а потом записывала под
    новым ключом (топология БД осела четырьмя фактами). Ключи стоят единицы
    токенов на факт и снимают оба промаха.

    Бюджет здесь — инвариант, а не пожелание: блок памяти физически не может
    вырасти вместе с базой. Факты идут от сильных к слабым, и когда бюджет
    исчерпан, хвост области схлопывается в одну строку — он остаётся достижим
    через recall_facts(scope=...), просто не занимает контекст даром.
    """
    if facts is None:
        return ""
    index = facts.index()
    if not index:
        return "\n\nПамять команды пуста."
    lines = _render_index(index, settings.memory_index_token_budget)
    return ("\n\nОглавление памяти команды (области знаний, ключи фактов и когда они "
            "пригодятся):\n" + "\n".join(lines))


def _render_index(index: list[dict], token_budget: int) -> list[str]:
    lines: list[str] = []
    used = 0
    for area in index:
        lines.append(f"- {area['scope']}:")
        used += len(lines[-1]) // 4 + 1
        shown = 0
        for fact in area["facts"]:
            line = f"  - {fact['key']}"
            # Урок и запрет — не факты об инфраструктуре, а выводы из неудач; без
            # пометки «перед деплоем проверь бэкап» читается как топология.
            if label := KIND_LABELS.get(fact["kind"], ""):
                line += f" ({label})"
            if fact["description"]:
                line += f" — {fact['description']}"
            cost = len(line) // 4 + 1
            if used + cost > token_budget:
                break
            lines.append(line)
            used += cost
            shown += 1
        left = len(area["facts"]) - shown
        if left:
            lines.append(f"  - ... ещё {left} (recall_facts(scope=\"{area['scope']}\"))")
    return lines


def _summary(content: str) -> str:
    """Итог задачи одной фразой для журнала.

    Формат ответа Директора требует итог первой строкой финального хода — берём её
    и не платим лишним вызовом модели за резюме.
    """
    line = next((s for s in (ln.strip() for ln in content.splitlines()) if s), "")
    return line[:300]


class Director(Agent):
    def __init__(self, llm: LLMClient, gateway=None,
                 memory=None, journal=None, skills: dict | None = None,
                 skills_dir: Path | None = None, agent_llm: LLMClient | None = None,
                 progress=None, facts: KnowledgeStore | None = None):
        # Модель временных агентов; без неё они работают на модели Директора.
        agent_llm = agent_llm or llm

        async def _make_report(title: str, markdown: str) -> dict:
            path = await asyncio.to_thread(
                save_report, settings.reports_dir, redact(title), redact(markdown)
            )
            self._report_path = path
            return {"saved": path, "note": "файл будет отправлен пользователю"}

        library = skills or {}

        async def _plan(title: str, steps: list[str]) -> dict:
            await progress.plan(self._run_id, title, steps)
            return {"shown": len(steps)}

        async def _remind_marks(sub: Agent, result, left: list[int]) -> None:
            """Прод 16.09: агенты на дешёвой модели делали работу, но mark_step не
            звали — просьба в конце промпта тонет за двадцатью вызовами, и пункты
            уходили в «пропущен». Один ход, где кроме mark_step ничего нет; ответ
            агента остаётся прежним, текст этого хода не нужен."""
            log.info("mark_step_reminder", agent=sub.name, steps=left)
            mark = sub._find_tool("mark_step")
            # Список — подсказка: сбой этого хода не должен выбросить готовую работу агента.
            try:
                msg = await agent_llm.chat(
                    [*result.transcript, {"role": "user", "content": (
                        f"Ты не отметил пункты плана {', '.join(map(str, left))}. Отметь каждый "
                        "mark_step: \"done\" — выполнен, \"failed\" — не удался, \"skipped\" — "
                        "не понадобился. Ответ заново не пиши."
                    )}],
                    [mark.schema()],
                )
            except Exception:
                log.warning("mark_step_reminder_failed", agent=sub.name)
                return
            self._sub_usage += msg.usage
            for tc in msg.tool_calls or []:
                if tc.function.name == "mark_step":
                    result.trace.append("mark_step")
                    await sub._run_safe(tc)

        async def _spawn(role: str, skills: list[str], task: str, steps: list[int] | None = None) -> dict:
            # библиотеку читаем с инстанса — write_skill подменяет её на ходу
            unknown = [s for s in skills if s not in self._library]
            if unknown:
                return {"error": f"неизвестные навыки: {unknown}", "available": list(self._library)}
            plan = progress.steps(self._run_id) if progress is not None else None
            steps = sorted(set(steps or []))
            # Без номеров пункты агента некому отметить, и они висят неотмеченными — поэтому не просьба
            # в промпте, а отказ: Директор исправит вызов.
            if plan is not None and (not steps or not all(1 <= n <= len(plan) for n in steps)):
                return {"error": "укажи steps — номера пунктов плана, которые выполняет агент",
                        "plan": {i + 1: text for i, text in enumerate(plan)}}
            chosen = [self._library[s] for s in skills]
            # Доступ к хосту складывается: агенту с tls+security нужен один host_query,
            # видящий бинарники обоих навыков, иначе он натыкался бы на отказы.
            access = reduce(or_, (s.access for s in chosen), HostAccess())
            resources = build_resource_tools(chosen)
            if (untrusted := [s.name for s in chosen if s.untrusted]) and (
                access.binaries
                or access.exec_allowed
                or any(t.safety is Safety.DANGEROUS for s in chosen for t in s.tools)
                # run_skill_script — чужой код в контейнере агентов
                or any(t.safety is Safety.DANGEROUS for t in resources)
                # скил, чьи инструменты строятся по доступу (ssh), даёт доступ к нодам
                or any(s.access_tools for s in chosen)
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
            # а шлюз на дубль имени в tools отвечает 400. Один и тот же инструмент
            # сводим в один; разные реализации под одним именем — отказ: выбор
            # «последний в dict» молча подменял бы вызываемый код (аудит F07).
            uniq: dict[str, Tool] = {}
            candidates = [
                *(t for s in chosen
                  for t in (_budgeted(s.tools, _UNTRUSTED_CALL_BUDGET) if s.untrusted else s.tools)),
                *(t for s in chosen if s.access_tools for t in s.access_tools(access)),
                *build_host_tools(access),
                *resources,
            ]
            if plan is not None:
                async def _mark_step(step: int, status: str) -> dict:
                    return await progress.mark(sub.agent_id, step, status)

                # Инструмент ядра, как host_query: навыки о нём не знают и его не объявляют.
                candidates.append(Tool(
                    "mark_step",
                    "Mark one of YOUR plan steps in the user's live TODO list as soon as it is "
                    "finished. Can be called in the same turn as other tools. Safe.",
                    MarkStepParams, _mark_step, Safety.SAFE,
                ))
            for t in candidates:
                if _identity(uniq.setdefault(t.name, t)) != _identity(t):
                    return {"error": (
                        f"инструмент {t.name} в выданных навыках реализован по-разному — "
                        "выдай эти навыки разным агентам"
                    )}
            prompt = compose_prompt(role, chosen)
            if plan is not None:
                prompt += "\n\n" + _steps_block({n: plan[n - 1] for n in steps})
            sub = Agent(
                name=f"spawned:{'+'.join(skills)}",
                system_prompt=prompt,
                tools=list(uniq.values()),
                llm=agent_llm,
                gateway=gateway,
                episode=self._episode,
            )
            # Вывод такого агента вернётся в контекст Директора: всё, что он запишет
            # в память по итогам этой задачи, уходит в карантин (аудит F09).
            self._untrusted_skills.update(s.name for s in chosen if s.untrusted)
            log.info("spawn", role=role, skills=skills, steps=steps)
            if plan is not None:
                await progress.started(self._run_id, steps, sub.agent_id)
            # Временный агент: не регистрируем в реестре, вызываем напрямую и забываем
            # вместе с контекстом. memory не передаём — истории у него быть не должно.
            try:
                result = await sub.handle(Task(content=task, run_id=self._run_id))
                if plan is not None and (left := progress.unmarked(sub.agent_id)):
                    await _remind_marks(sub, result, left)
            finally:
                if plan is not None:
                    await progress.finished(sub.agent_id)
            self._sub_trace.extend(result.trace)
            self._sub_usage += result.usage
            self._agents_used.append(sub.name)
            return {"agent": sub.name, "result": result.content, "success": result.success}

        async def _write_skill(name: str, description: str, instructions: str,
                               overwrite: bool = False) -> dict:
            if not (3 <= len(name) <= 64 and _SKILL_NAME.fullmatch(name)):
                return {"error": "имя навыка: латиница kebab-case (weekly-report), 3-64 символа"}
            if len(description) > 1024 or not description.strip():
                return {"error": "description: одна строка до 1024 символов"}
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

        async def _write_skill_precheck(args: dict) -> str | None:
            """До подтверждения: Директор видит у навыка только description, и
            перезапись вслепую молча стёрла бы старые шаги и грабли. Отдаём текущий
            плейбук — сведёт старое с новым и позовёт снова с overwrite."""
            if not _SKILL_NAME.fullmatch(args["name"]):
                return None  # имя отклонит сам _write_skill — путь из него не строим
            d = skills_dir / args["name"]
            if (d / "tools.py").exists():
                return f"навык {args['name']} содержит код — его плейбук правит человек"
            md = d / "SKILL.md"
            if args["overwrite"] or not md.exists():
                return None
            current = await asyncio.to_thread(md.read_text, encoding="utf-8")
            return (f"навык {args['name']} уже есть. Его текущий текст ниже: сведи старые "
                    f"шаги и грабли с новыми и вызови снова с overwrite=true.\n\n{current}")

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
        tools = [report_tool]
        if facts is not None:
            tools += memory_tools(facts, self._provenance)
        if library:
            tools.append(spawn_tool)
        if library and progress is not None:
            tools.append(Tool(
                name="plan",
                description=(
                    "Show the user a live TODO list of this task in Telegram. Call before the "
                    "first spawn; call again to revise. Safe."
                ),
                params_model=PlanParams,
                fn=_plan,
                safety=Safety.SAFE,
            ))
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
                precheck=_write_skill_precheck,
            ))
        if journal is not None:
            async def _recall_experience(query: str) -> dict:
                return {"episodes": await asyncio.to_thread(journal.search, query)}

            tools.append(Tool(
                name="recall_experience",
                description=(
                    "Recall how similar tasks were solved before: past intents, one-line "
                    "outcomes, skills used, and what went wrong then — refused confirmations, "
                    "tool errors, plan steps left undone. Safe."
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
                with_plan=progress is not None,
            ),
            tools=tools,
            llm=llm,
            gateway=gateway,
            memory=memory,
        )
        self._library = library
        self._facts = facts
        self._skills_dir = skills_dir
        self._base_prompt = self.system_prompt
        self._journal = journal
        self._progress = progress
        # Задачи Директора идут строго по одной: накопители ниже живут на инстансе,
        # параллельный handle их бы перемешал. Спавнутых агентов замок не касается —
        # они выполняются внутри одной задачи и параллелятся намеренно.
        self._lock = asyncio.Lock()
        self._untrusted_skills: set[str] = set()
        self._sub_trace: list[str] = []
        # Цена спавнутых агентов — отдельно от своей: спавн платит дешёвой моделью,
        # и без разделения не видно, за что уходят токены.
        self._sub_usage = Usage()
        self._agents_used: list[str] = []
        self._report_path: str = ""
        self._run_id: str = ""

    def _provenance(self) -> dict:
        """Происхождение записей памяти в текущей задаче: задача журнала, а в
        `source` — недоверенные навыки, если они работали (пусто — не работали).
        Флаг на весь run консервативен."""
        source = ("spawn:" + ",".join(sorted(self._untrusted_skills))
                  if self._untrusted_skills else "")
        return {"run_id": self._run_id, "source": source}

    def reload_library(self, skills: dict) -> None:
        """Подхватить обновлённые навыки без рестарта процесса."""
        self._library = skills
        self._base_prompt = build_director_prompt(
            {n: s.description for n, s in self._library.items()},
            with_experience=self._journal is not None,
            with_write_skill=self._skills_dir is not None,
            with_plan=self._progress is not None,
        )

    async def handle(self, task: Task) -> Result:
        async with self._lock:
            self._sub_trace = []
            self._sub_usage = Usage()
            self._agents_used = []
            self._untrusted_skills = set()
            self._report_path = ""
            self._run_id = task.run_id or task.id
            self._episode = Episode()
            self.system_prompt = self._base_prompt + await asyncio.to_thread(
                _memory_index, self._facts)
            started = time.monotonic()
            # Отмена задачи (BaseException) сюда не попадёт — журналу тогда нечего
            # писать, и None это говорит вместо падения в finally.
            result: Result | None = None
            try:
                result = await super().handle(task)
            except Exception as e:
                # Упавшая задача — самый ценный эпизод, и раньше она в журнал не
                # попадала вовсе: ответ пользователю рисует обработчик сообщения.
                self._episode.broke(f"задача оборвалась: {type(e).__name__}: {e}")
                result = Result(task_id=task.id, content=str(e), success=False)
                raise
            finally:
                # «Yes to all» живёт до конца ответа — и при ошибке, и при отмене.
                if self._gateway is not None:
                    self._gateway.release(self._run_id)
                if self._progress is not None:
                    self._episode.plan_left(await self._progress.finish(self._run_id))
                if self._journal is not None and result is not None:
                    await self._write_journal(
                        task, result,
                        duration_ms=int((time.monotonic() - started) * 1000))
            result.attachment = self._report_path
            return result

    async def _write_journal(self, task: Task, result: Result, duration_ms: int = 0) -> None:
        try:
            await asyncio.to_thread(
                self._journal.record,
                task_id=task.id,
                chat_id=task.chat_id,
                intent=redact(task.content),
                agents=self._agents_used,
                tool_seq=result.trace + self._sub_trace,
                iterations=result.iterations,
                success=result.success,
                summary=_summary(result.final or result.content),
                director_in=result.usage.prompt_tokens,
                director_out=result.usage.completion_tokens,
                agents_in=self._sub_usage.prompt_tokens,
                agents_out=self._sub_usage.completion_tokens,
                cost=result.usage.cost + self._sub_usage.cost,
                llm_calls=result.usage.calls + self._sub_usage.calls,
                duration_ms=duration_ms,
                director_cached=result.usage.cached_tokens,
                agents_cached=self._sub_usage.cached_tokens,
                outcome=self._episode.outcome(),
                problems=[redact(p) for p in self._episode.problems()],
            )
            await asyncio.to_thread(
                self._journal.save_transcript,
                task.id,
                redact(json.dumps(result.transcript, ensure_ascii=False, indent=2)),
                settings.journal_transcripts,
            )
        except Exception:
            log.exception("journal_write_failed", task_id=task.id)
