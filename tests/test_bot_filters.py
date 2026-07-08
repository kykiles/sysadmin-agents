from unittest.mock import MagicMock
from app.bot.filters import WhitelistFilter


def _user_msg(uid):
    m = MagicMock()
    m.from_user = MagicMock(id=uid)
    return m


async def test_whitelist_allows_known_user():
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(42)) is True


async def test_whitelist_blocks_unknown_user():
    f = WhitelistFilter(allowed_id=42)
    assert await f(_user_msg(999)) is False
