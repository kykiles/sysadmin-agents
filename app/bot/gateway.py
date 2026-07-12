import asyncio
from aiogram import Bot
from app.agents.registry import ConfirmationGateway
from app.agents.messages import ConfirmationRequest, Decision
from app.bot.keyboards import approve_keyboard
from app.bot.render import format_confirmation
from app.config import settings


class TelegramConfirmationGateway(ConfirmationGateway):
    def __init__(self, bot: Bot, chat_id: int, timeout: int | None = None):
        self._bot = bot
        self._chat_id = chat_id
        self._timeout = timeout if timeout is not None else settings.confirmation_timeout_seconds
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self._scoped: set[str] = set()

    def _resolve(self, task_id: str, decision: Decision) -> None:
        fut = self._pending.get(task_id)
        if fut and not fut.done():
            fut.set_result(decision)

    def release(self, task_id: str) -> None:
        self._scoped.discard(task_id)

    async def request(self, req: ConfirmationRequest) -> Decision:
        if req.task_id in self._scoped:
            await self._bot.send_message(
                self._chat_id, f"🔓 Авто-одобрено: {req.tool_name} {req.args}"
            )
            return Decision.AUTO_APPROVED
        fut = asyncio.get_running_loop().create_future()
        self._pending[req.task_id] = fut
        text = format_confirmation(req)
        await self._bot.send_message(
            self._chat_id, text, reply_markup=approve_keyboard(req.task_id, req.tool_name)
        )
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            return Decision.REJECTED
        finally:
            self._pending.pop(req.task_id, None)
