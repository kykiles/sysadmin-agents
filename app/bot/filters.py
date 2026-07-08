from aiogram.filters import BaseFilter
from aiogram.types import Message


class WhitelistFilter(BaseFilter):
    def __init__(self, allowed_id: int):
        self._allowed = allowed_id

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id == self._allowed
