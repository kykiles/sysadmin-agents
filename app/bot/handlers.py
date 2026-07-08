from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from app.agents.messages import Task
from app.agents.registry import AgentRegistry
from app.bot.filters import WhitelistFilter


def build_router(*, registry: AgentRegistry, allowed_id: int, chat_id: int) -> Router:
    router = Router()

    @router.message(WhitelistFilter(allowed_id), Command("start"))
    async def _start(message: Message):
        await message.answer("Система активна. Опишите задачу.")

    @router.message(WhitelistFilter(allowed_id))
    async def _task(message: Message, registry: AgentRegistry):
        task = Task(content=message.text or "")
        result = await registry.request("director", task)
        await message.answer(result.content)

    @router.callback_query(F.data.startswith("cf:"))
    async def _confirm(callback: CallbackQuery, registry: AgentRegistry):
        _, task_id, decision = callback.data.split(":")
        from app.bot.gateway import TelegramConfirmationGateway
        gw = registry._gateway
        if isinstance(gw, TelegramConfirmationGateway):
            gw._resolve(task_id, decision == "yes")
        await callback.answer("Подтверждено" if decision == "yes" else "Отклонено")
        await callback.message.edit_text(
            callback.message.text + f"\n\n> Решение: {'Approve' if decision == 'yes' else 'Reject'}"
        )

    return router
