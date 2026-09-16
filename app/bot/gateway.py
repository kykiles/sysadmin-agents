import asyncio
import secrets
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.agents.messages import ConfirmationRequest, Decision
from app.bot.keyboards import approve_keyboard
from app.bot.render import TELEGRAM_LIMIT, confirmation_details, format_confirmation
from app.config import settings
from app.logging import get_logger

log = get_logger("gateway")


@dataclass
class _Pending:
    future: asyncio.Future
    user_id: int
    chat_id: int
    message_id: int
    expires_at: float
    run_id: str
    scope: str | None


class TelegramConfirmationGateway:
    """Спрашивает владельца об одном конкретном вызове инлайн-кнопками.

    Каждый запрос получает случайный одноразовый request_id. Кнопка разрешает
    только его, только владельцу, только из того самого сообщения и только пока
    запрос ждёт; решение гасит запрос сразу. Таймаут, отмена и ошибка отправки
    снимают запрос, поэтому старая или повторная кнопка ничего не разрешает.
    Раньше согласие было привязано к task ID, и кнопка истёкшего запроса одобряла
    следующую операцию той же задачи (аудит 2026-09-12, F04).

    «Yes to all» разрешает вызовы с тем же scope (инструмент + цель + программа)
    только внутри того же run и только до release(run_id) — Директор зовёт его,
    закончив ответ. Сотня одинаковых кнопок подряд приучала жать не читая.
    """

    def __init__(self, bot: Bot, chat_id: int, timeout: int | None = None):
        self._bot = bot
        # Личный чат с владельцем: его id совпадает с id пользователя.
        self._chat_id = chat_id
        self._timeout = timeout if timeout is not None else settings.confirmation_timeout_seconds
        self._pending: dict[str, _Pending] = {}
        self._grants: dict[str, set[str]] = {}

    def resolve(self, request_id: str, decision: Decision, *, user_id: int,
                chat_id: int, message_id: int) -> bool:
        """Погасить запрос решением из кнопки. False — кнопка ничего не решила:
        запроса нет (истёк, решён, чужой формат) или нажата не там и не тем."""
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        if (user_id, chat_id, message_id) != (pending.user_id, pending.chat_id, pending.message_id):
            return False
        if asyncio.get_running_loop().time() > pending.expires_at:
            return False
        if decision is Decision.APPROVED_ALL:
            if pending.scope is None:
                return False
            self._grants.setdefault(pending.run_id, set()).add(pending.scope)
        del self._pending[request_id]
        pending.future.set_result(decision)
        return True

    def release(self, run_id: str) -> None:
        """Ответ готов: «Yes to all» этого run больше ничего не разрешает."""
        self._grants.pop(run_id, None)

    async def request(self, req: ConfirmationRequest) -> Decision:
        scope = req.scope()
        if scope is not None and scope in self._grants.get(req.run_id, ()):
            log.info("confirmation_granted", tool=req.tool_name, scope=scope, run_id=req.run_id)
            return Decision.AUTO_APPROVED
        request_id = secrets.token_urlsafe(9)
        details = confirmation_details(req)
        text = format_confirmation(req, request_id, details)
        attached = len(text) > TELEGRAM_LIMIT
        try:
            if attached:
                # Полный текст — до кнопок: подтвердить непоказанный хвост нельзя (F05).
                await self._bot.send_document(
                    self._chat_id,
                    BufferedInputFile(details.encode("utf-8"), filename=f"confirm-{request_id}.txt"),
                    caption=f"Запрос {request_id}: полный текст. Исполнен будет ровно он.",
                )
                text = format_confirmation(req, request_id, details, attached=True)
            msg = await self._bot.send_message(
                self._chat_id, text, reply_markup=approve_keyboard(request_id, with_all=scope is not None)
            )
        except Exception:
            # Недоставленное подтверждение — не согласие: исполнения не будет.
            log.exception("confirmation_not_delivered", tool=req.tool_name, request_id=request_id)
            return Decision.REJECTED
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._pending[request_id] = _Pending(
            fut, self._chat_id, self._chat_id, msg.message_id, loop.time() + self._timeout,
            req.run_id, scope,
        )
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            return Decision.REJECTED
        finally:
            # и при отмене родителя: ждущий запрос не переживает свою корутину
            self._pending.pop(request_id, None)
