import asyncio
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from app.agents.messages import Task, Decision
from app.agents.registry import AgentRegistry
from app.bot.filters import WhitelistFilter
from app.bot.keyboards import review_markup
from app.bot.render import render_answer, split_message
from app.learning.review import render_review, resolve_candidate, resolve_fact, run_review


def build_router(*, registry: AgentRegistry, allowed_id: int, memory, learning=None,
                 reload_library=None) -> Router:
    router = Router()

    @router.message(WhitelistFilter(allowed_id), Command("start"))
    async def _start(message: Message):
        await message.answer("Система активна. Опишите задачу.")

    @router.message(WhitelistFilter(allowed_id), Command("help"))
    async def _help(message: Message):
        await message.answer(render_answer(
            "Опишите задачу обычным текстом — Директор разберёт её и делегирует "
            "специалисту (Docker, БД, хост). Опасные операции требуют подтверждения.\n\n"
            "**Команды**\n"
            "> /start — проверить, что система активна\n"
            "> /help — эта справка\n"
            "> /reset — очистить историю диалога\n"
            "> /learn — самопроверка: повторяющиеся задачи и устаревшие знания\n"
            "> /reload — перечитать навыки и специалистов после загрузки новых\n\n"
            "Нужен отчёт файлом — попросите «оформи отчёт»."
        ))

    @router.message(WhitelistFilter(allowed_id), Command("reset"))
    async def _reset(message: Message):
        await asyncio.to_thread(memory.clear, str(message.chat.id))
        await message.answer("История диалога очищена.")

    @router.message(WhitelistFilter(allowed_id), Command("reload"))
    async def _reload(message: Message):
        if reload_library is None:
            await message.answer("Перезагрузка библиотеки недоступна.")
            return
        try:
            summary = await asyncio.to_thread(reload_library)
        except Exception as e:
            await message.answer(f"Не перезагрузил: {e}")
            return
        await message.answer(f"Библиотека перечитана — {summary}.")

    @router.message(WhitelistFilter(allowed_id), Command("learn"))
    async def _learn(message: Message):
        if learning is None:
            await message.answer("Самопроверка выключена (нет журнала задач).")
            return
        outcome = await run_review(learning)
        if outcome.is_empty:
            await message.answer("Нечего предложить: повторов и устаревших фактов не нашёл.")
            return
        await message.answer(
            render_answer(render_review(outcome)), reply_markup=review_markup(outcome)
        )

    @router.callback_query(F.data.startswith("lc:"))
    async def _reject_candidate(callback: CallbackQuery):
        _, sid, _choice = callback.data.split(":")
        signature = resolve_candidate(learning.candidates, sid) if learning else None
        if signature is not None:
            learning.candidates.set_status(signature, "rejected")
        await callback.answer("Больше не предложу" if signature else "Кандидат не найден")

    @router.callback_query(F.data.startswith("lf:"))
    async def _forget_fact(callback: CallbackQuery):
        _, sid, _choice = callback.data.split(":")
        found = resolve_fact(learning.facts, sid) if learning else None
        if found is not None:
            learning.facts.forget(*found)
        await callback.answer("Факт забыт" if found else "Факт не найден")

    @router.message(WhitelistFilter(allowed_id))
    async def _task(message: Message):
        task = Task(content=message.text or "", chat_id=str(message.chat.id))
        result = await registry.request("director", task)
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
        for part in split_message(result.content):
            await message.answer(render_answer(part))

    @router.callback_query(F.data.startswith("cf:"))
    async def _confirm(callback: CallbackQuery):
        _, task_id, choice = callback.data.split(":")
        from app.bot.gateway import TelegramConfirmationGateway
        gw = registry._gateway
        if isinstance(gw, TelegramConfirmationGateway):
            if choice == "all":
                gw._scoped.add(task_id)
                gw._resolve(task_id, Decision.APPROVED)
            elif choice == "yes":
                gw._resolve(task_id, Decision.APPROVED)
            else:
                gw._resolve(task_id, Decision.REJECTED)
        labels = {"yes": "Выполнить", "no": "Отмена", "all": "Выполнить всё без вопросов"}
        await callback.answer(labels.get(choice, choice))
        # html_text сохраняет уже отрендеренную разметку исходного сообщения;
        # callback.message.text отдал бы её плоским текстом и потерял бы оформление
        await callback.message.edit_text(
            callback.message.html_text + f"\n\n<b>Решение:</b> {labels.get(choice, choice)}"
        )

    return router
