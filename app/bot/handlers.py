import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from app.agents.messages import Task, Decision
from app.agents.registry import AgentRegistry
from app.bot.filters import WhitelistFilter


def build_router(*, registry: AgentRegistry, allowed_id: int, memory) -> Router:
    router = Router()

    @router.message(WhitelistFilter(allowed_id), Command("start"))
    async def _start(message: Message):
        await message.answer("Система активна. Опишите задачу.")

    @router.message(WhitelistFilter(allowed_id), Command("reset"))
    async def _reset(message: Message):
        await asyncio.to_thread(memory.clear)
        await message.answer("История диалога очищена.")

    @router.message(WhitelistFilter(allowed_id))
    async def _task(message: Message):
        task = Task(content=message.text or "")
        result = await registry.request("director", task)
        await message.answer(result.content, parse_mode=None)

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
        labels = {"yes": "Approve", "no": "Reject", "all": "Разрешено всё в задаче"}
        await callback.answer(labels.get(choice, choice))
        await callback.message.edit_text(
            callback.message.text + f"\n\n> Решение: {labels.get(choice, choice)}",
            parse_mode=None,
        )

    return router
