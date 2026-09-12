from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InaccessibleMessage, Message, User


def _owner_in_private(user: User | None, message, allowed_id: int) -> bool:
    """Персональный бот: только владелец и только в личном чате с ним.

    Группы не поддерживаются намеренно: кнопки памяти и подтверждений в общем чате
    нажимал бы любой участник (аудит 2026-09-12, F06). Сообщение callback'а может
    быть недоступно (старше 48 часов) — тогда чат не проверить, и это отказ.
    """
    if user is None or user.id != allowed_id:
        return False
    if message is None or isinstance(message, InaccessibleMessage):
        return False
    return message.chat.type == ChatType.PRIVATE and message.chat.id == allowed_id


class WhitelistFilter(BaseFilter):
    def __init__(self, allowed_id: int):
        self._allowed = allowed_id

    async def __call__(self, message: Message) -> bool:
        return _owner_in_private(message.from_user, message, self._allowed)


class OwnerCallbackFilter(BaseFilter):
    def __init__(self, allowed_id: int):
        self._allowed = allowed_id

    async def __call__(self, callback: CallbackQuery) -> bool:
        return _owner_in_private(callback.from_user, callback.message, self._allowed)
