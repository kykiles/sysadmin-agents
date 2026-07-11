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
