"""Консолидация: обобщение прошедших задач в предложения записать факт.

Директор пишет память в момент задачи и неизбежно что-то не записывает — решение
казалось разовым, а повторилось. Раз в сутки проходим по журналу и предлагаем то,
чего в памяти нет. Предлагаем, а не пишем: LLM задним числом знает хуже, чем
Директор в моменте (ровно на этом умер детектор повторов, см. docs/adr/0004), и
человек должен видеть, что оседает в памяти навсегда.

Вход — журнал (интент, итог, эпизод), а не транскрипты: транскриптов хранится два
десятка, за сутки задач может быть больше, и полный ход двадцати задач стоит
десятки тысяч токенов там, где хватает двух строк на задачу.
"""
import json
import logging
from typing import Any, Protocol

from agent_memory.facts import KINDS, KnowledgeStore
from agent_memory.journal import TaskJournal

log = logging.getLogger(__name__)


class LLM(Protocol):
    """Всё, что модулю нужно от модели: один ход без инструментов."""

    async def chat(self, messages: list[dict]) -> Any: ...


_PROMPT = (
    "Ты разбираешь журнал работы админской системы за прошедшие сутки и решаешь, "
    "чего не хватает в её долговременной памяти.\n\n"
    "Оглавление памяти (области и ключи фактов):\n{index}\n\n"
    "Задачи за период (что просили → чем кончилось; строки «не вышло» — что по "
    "дороге отказало):\n{tasks}\n\n"
    "Предложи не больше {limit} записей, которые стоит запомнить:\n"
    '- kind "stable" — топология, пути, принятые решения, договорённости; '
    'kind "snapshot" — значения, которые сами меняются (версии, порты, размеры);\n'
    '- kind "lesson" — «перед X проверь Y»: чего не хватило в задаче, которая '
    "спотыкалась;\n"
    '- kind "negative_rule" — «при X не делай Y, не помогает»: путь, который '
    "в этих задачах не сработал.\n"
    "Урок и запрет предлагай только по строкам «не вышло», и только если то же "
    "повторилось или стоило задаче захода впустую — из единичной ошибки правила не "
    "делай. Не предлагай: разовые находки (что было в этих логах, почему упал этот "
    "запрос), то, что уже есть в оглавлении, и то, что легко узнать заново одной "
    "командой.\n"
    "Ответь ТОЛЬКО массивом JSON, без пояснений: "
    '[{{"scope": "тема", "key": "snake_case", "value": "значение", '
    '"description": "когда пригодится", "kind": "stable"}}]. '
    "Пустой массив — нормальный ответ."
)


def _render_index(index: list[dict]) -> str:
    if not index:
        return "(пусто)"
    return "\n".join(
        f"- {a['scope']}: {', '.join(f['key'] for f in a['facts'])}" for a in index
    )


def _render_tasks(tasks: list[dict]) -> str:
    lines: list[str] = []
    for t in tasks:
        line = f"- {t['intent']} → {t.get('summary') or ''}"
        if (outcome := t.get("outcome")) and outcome != "ok":
            line += f" [{outcome}]"
        lines.append(line)
        lines.extend(f"  не вышло: {p}" for p in t.get("problems") or [])
    return "\n".join(lines)


def _parse(content: str, limit: int) -> list[dict]:
    raw = (content or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        log.warning("consolidate_bad_json")
        return []
    out = []
    for it in items[:limit]:
        if isinstance(it, dict) and it.get("scope") and it.get("key") and it.get("value"):
            # Незнакомый kind не пропускаем в память: сроки перепроверки в lint'е и
            # пометка в оглавлении держатся на известном наборе.
            kind = str(it.get("kind", "stable"))
            out.append({"scope": str(it["scope"]), "key": str(it["key"]),
                        "value": str(it["value"]), "description": str(it.get("description", "")),
                        "kind": kind if kind in KINDS else "stable"})
    return out


async def propose(llm: LLM, journal: TaskJournal, facts: KnowledgeStore, *,
                  hours: int, limit: int) -> list[dict]:
    """Что стоило бы помнить по итогам последних `hours` часов. Ничего не пишет."""
    tasks = journal.recent_with_summary(hours)
    if not tasks:
        return []
    index = facts.index()
    known = {(a["scope"], f["key"]) for a in index for f in a["facts"]}
    prompt = _PROMPT.format(index=_render_index(index), tasks=_render_tasks(tasks), limit=limit)
    msg = await llm.chat([{"role": "user", "content": prompt}])
    return [p for p in _parse(msg.content, limit) if (p["scope"], p["key"]) not in known]
