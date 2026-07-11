import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.bot.gateway import TelegramConfirmationGateway
from app.agents.messages import ConfirmationRequest, Decision


async def test_gateway_returns_approved():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=123, timeout=5)
    asyncio.get_event_loop().call_later(0.05, lambda: gw._resolve("x", Decision.APPROVED))
    res = await gw.request(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={"container": "bot"}, description="restart bot"))
    assert res is Decision.APPROVED


async def test_gateway_returns_rejected_on_timeout():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=123, timeout=0)
    res = await gw.request(ConfirmationRequest(task_id="x", tool_name="docker_restart", args={}, description="d"))
    assert res is Decision.REJECTED


async def test_scoped_task_auto_approves_without_buttons():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=123, timeout=5)
    gw._scoped.add("t1")
    res = await gw.request(ConfirmationRequest(task_id="t1", tool_name="shell_exec", args={"cmd": "rm x"}, description="d"))
    assert res is Decision.AUTO_APPROVED
    _, kwargs = bot.send_message.call_args
    assert kwargs.get("reply_markup") is None
    assert "Авто-одобрено" in bot.send_message.call_args[0][1]


async def test_release_clears_scope():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=123, timeout=5)
    gw._scoped.add("t1")
    gw.release("t1")
    assert "t1" not in gw._scoped
    gw.release("missing")
