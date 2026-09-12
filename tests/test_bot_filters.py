from unittest.mock import MagicMock

import pytest
from aiogram.types import Chat, InaccessibleMessage

from app.bot.filters import OwnerCallbackFilter, WhitelistFilter


def _user_msg(uid, chat_type="private", chat_id=None):
    m = MagicMock()
    m.from_user = MagicMock(id=uid)
    m.chat.type = chat_type
    m.chat.id = uid if chat_id is None else chat_id
    return m


async def test_whitelist_allows_known_user():
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(42)) is True


async def test_whitelist_blocks_unknown_user():
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(999)) is False


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_whitelist_blocks_owner_outside_private_chat(chat_type):
    """Персональный бот: в группе кнопки нажимал бы любой участник (аудит F06)."""
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(42, chat_type=chat_type, chat_id=-100)) is False


async def test_whitelist_blocks_message_without_user():
    m = _user_msg(42)
    m.from_user = None
    assert await WhitelistFilter(allowed_id=42)(m) is False


def _callback(uid, message):
    cb = MagicMock()
    cb.from_user = MagicMock(id=uid)
    cb.message = message
    return cb


async def test_callback_allows_owner_in_private_chat():
    assert await OwnerCallbackFilter(42)(_callback(42, _user_msg(42))) is True


@pytest.mark.parametrize("cb", [
    _callback(999, _user_msg(42)),                                     # чужой пользователь
    _callback(42, _user_msg(42, chat_type="group", chat_id=-100)),     # владелец в группе
    _callback(42, None),                                               # сообщения нет
    _callback(42, InaccessibleMessage(chat=Chat(id=42, type="private"), message_id=1)),
])
async def test_callback_refused(cb):
    assert await OwnerCallbackFilter(42)(cb) is False
