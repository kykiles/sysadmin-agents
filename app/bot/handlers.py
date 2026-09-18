import asyncio
import html
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InaccessibleMessage, InlineKeyboardMarkup,
                           InputRichMessage, Message)
from app.agents.messages import Decision, Task, Result
from app.bot.filters import OwnerCallbackFilter, WhitelistFilter
from app.config import settings
from app.bot.keyboards import review_markup
from app.bot.render import render_answer, split_message
from app.learning.review import render_review, resolve_fact, run_review
from app.logging import get_logger

log = get_logger("handlers")


async def settle_review_button(callback: CallbackQuery, result: str) -> None:
    """Убрать решённую кнопку из сводки /learn и дописать итог в текст.

    Повторное нажатие и так ничего не пишет в память — но живая кнопка обещает
    обратное, а сводка не показывает, что уже решено. Ряд уходит целиком: у
    карантина в нём пара «Принять/Отклонить». Итог пишем и для устаревшей кнопки —
    она мертва, висеть ей незачем.
    """
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        return
    rows = msg.reply_markup.inline_keyboard if msg.reply_markup else []
    pressed = next((row for row in rows if any(b.callback_data == callback.data for b in row)), None)
    left = [row for row in rows if row is not pressed]
    # Предмет — из первой кнопки ряда: «Записать: infra/k» → «infra/k».
    subject = pressed[0].text.split(": ", 1)[-1] if pressed else ""
    note = f"{result}: {subject}" if subject else result
    try:
        await msg.edit_text(
            msg.html_text + f"\n• {html.escape(note)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=left) if left else None,
        )
    except Exception:
        log.warning("review_edit_failed", data=callback.data)


def with_quote(message: Message) -> str:
    """Reply на прошлый ответ: подставляем цитату в текст задачи.

    Старый ответ мог выпасть из окна истории по бюджету токенов — цитата
    возвращает его в текущий запрос. message.quote — выделенный фрагмент,
    если пользователь выделил часть ответа.
    """
    text = message.text or ""
    quoted = message.quote.text if message.quote else None
    if not quoted and message.reply_to_message:
        src = message.reply_to_message
        quoted = src.text or src.caption
    if not quoted:
        return text
    return (
        "Пользователь уточняет по этому фрагменту прошлого ответа:\n"
        f"<<<\n{quoted}\n>>>\n\n{text}"
    )


def build_router(*, director, gateway=None, allowed_id: int, memory, learning=None,
                 journal=None) -> Router:
    router = Router()
    # Один фильтр на все сообщения и все кнопки (cf:, lf:, sf:): владелец в личном
    # чате. Раньше whitelist стоял только на сообщениях, callbacks проверяли
    # лишь префикс (аудит 2026-09-12, F06).
    router.message.filter(WhitelistFilter(allowed_id))
    router.callback_query.filter(OwnerCallbackFilter(allowed_id))

    @router.message(Command("start"))
    async def _start(message: Message):
        await message.answer("Система активна. Опишите задачу.")

    @router.message(Command("help"))
    async def _help(message: Message):
        await message.answer(render_answer(
            "Опишите задачу обычным текстом — Директор разберёт её и под задачу "
            "соберёт временных агентов из навыков (Docker, БД, хост). "
            "Опасные операции требуют подтверждения.\n\n"
            "**Команды**\n"
            "> /start — проверить, что система активна\n"
            "> /help — эта справка\n"
            "> /reset — очистить историю диалога\n"
            "> /learn — самопроверка памяти: давно не подтверждавшиеся знания, факты из "
            "карантина и предложения запомнить новое — решаете кнопками\n"
            "> /trace [N] — ход N-й с конца задачи файлом (по умолчанию последней)\n\n"
            "Нужен отчёт файлом — попросите «оформи отчёт»."
        ))

    @router.message(Command("reset"))
    async def _reset(message: Message):
        await asyncio.to_thread(memory.clear, str(message.chat.id))
        await message.answer("История диалога очищена.")

    @router.message(Command("trace"))
    async def _trace(message: Message):
        if journal is None:
            await message.answer("Журнал выключен — транскриптов нет.")
            return
        arg = (message.text or "").split(maxsplit=1)
        back = int(arg[1]) if len(arg) > 1 and arg[1].isdigit() else 1
        row = await asyncio.to_thread(journal.transcript, back)
        if row is None:
            await message.answer("Столько задач в журнале нет.")
            return
        task_id, body = row
        path = Path(settings.reports_dir) / f"trace-{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, body, "utf-8")
        try:
            await message.answer_document(FSInputFile(path), caption=f"Задача {task_id}")
        finally:
            path.unlink(missing_ok=True)

    @router.message(Command("learn"))
    async def _learn(message: Message):
        if learning is None:
            await message.answer("Самопроверка выключена (нет журнала задач).")
            return
        outcome = await run_review(learning)
        if outcome.is_empty:
            await message.answer("Нечего предложить: устаревших фактов не нашёл.")
            return
        await message.answer(
            render_answer(render_review(outcome)), reply_markup=review_markup(outcome)
        )

    @router.callback_query(F.data.startswith("sf:"))
    async def _remember_suggested(callback: CallbackQuery):
        _, sid, _choice = callback.data.split(":")
        fact = learning.pending.pop(sid, None) if learning else None
        if fact is not None:
            # kind доезжает до записи: урок и запрет иначе осели бы в памяти
            # обычными фактами — без пометки в оглавлении и со сроком stable.
            learning.facts.remember(fact["scope"], fact["key"], fact["value"],
                                    fact.get("kind", "stable"),
                                    description=fact.get("description", ""),
                                    origin="consolidation")
        result = "Записано" if fact else "Предложение устарело"
        await callback.answer(result)
        await settle_review_button(callback, result)

    @router.callback_query(F.data.startswith("lf:"))
    async def _forget_fact(callback: CallbackQuery):
        _, sid, _choice = callback.data.split(":")
        found = resolve_fact(learning.facts, sid) if learning else None
        if found is not None:
            learning.facts.forget(*found)
        result = "Факт забыт" if found else "Факт не найден"
        await callback.answer(result)
        await settle_review_button(callback, result)

    @router.callback_query(F.data.startswith("qf:"))
    async def _resolve_proposal(callback: CallbackQuery):
        parts = callback.data.split(":")
        if learning is None or len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("ok", "no"):
            await callback.answer("Кнопка устарела")
            return
        pid = int(parts[1])
        if parts[2] == "ok":
            done = learning.facts.approve(pid) is not None
            label = "Принято в память"
        else:
            done = learning.facts.reject(pid)
            label = "Отклонено"
        result = label if done else "Предложение устарело или уже решено"
        await callback.answer(result)
        await settle_review_button(callback, result)

    @router.message()
    async def _task(message: Message):
        task = Task(content=with_quote(message), chat_id=str(message.chat.id))
        try:
            result = await director.handle(task)
        except Exception as e:
            log.exception("task_failed", task_id=task.id)
            result = Result(task_id=task.id, content=f"error: {e}", success=False)
        if result.attachment:
            # подпись режем ДО рендера: обрезка готового HTML разорвала бы тег
            caption = split_message(result.content, limit=700)[0]
            try:
                await message.answer_document(
                    FSInputFile(result.attachment),
                    caption=render_answer(caption),
                )
            finally:
                # отчёт нужен только для отправки — на сервере не копим
                Path(result.attachment).unlink(missing_ok=True)
            return
        # Разметку модели Telegram рисует сам (таблицы, списки, код) — Bot API 10.1.
        # Отказ (невалидная разметка, длина) — прежний HTML, ответ не теряется.
        try:
            await message.bot.send_rich_message(
                chat_id=message.chat.id, rich_message=InputRichMessage(markdown=result.content))
            return
        except Exception:
            log.warning("rich_answer_failed", task_id=task.id)
        for part in split_message(result.content):
            await message.answer(render_answer(part))

    @router.callback_query(F.data.startswith("cf:"))
    async def _confirm(callback: CallbackQuery):
        parts = callback.data.split(":")
        # Чужие варианты ничего не решают; старая cf:<task_id>:all не найдёт запроса.
        choices = {"yes": Decision.APPROVED, "all": Decision.APPROVED_ALL, "no": Decision.REJECTED}
        if len(parts) != 3 or parts[2] not in choices:
            await callback.answer("Кнопка устарела")
            return
        _, request_id, choice = parts
        decision = choices[choice]
        resolved = gateway is not None and gateway.resolve(
            request_id, decision, user_id=callback.from_user.id,
            chat_id=callback.message.chat.id, message_id=callback.message.message_id,
        )
        if not resolved:
            await callback.answer("Запрос устарел или уже решён — ничего не выполнено")
            return
        label = {"yes": "Да", "all": "Да, для всех таких", "no": "Нет"}[choice]
        await callback.answer(label)
        # Запрос уже погашен: не удалось убрать кнопки — повторное нажатие всё равно
        # ничего не решит. html_text сохраняет разметку исходного сообщения.
        try:
            await callback.message.edit_text(
                callback.message.html_text + f"\n\n<b>Решение:</b> {label}"
            )
        except Exception:
            log.warning("confirmation_edit_failed", request_id=request_id)

    return router
