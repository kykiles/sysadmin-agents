import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.agents.registry import AgentRegistry
from app.agents.messages import Task, Result, Decision
from app.bot.gateway import TelegramConfirmationGateway


class FakeDirector:
    name = "director"
    def __init__(self, result_text):
        self._result_text = result_text
    async def handle(self, task: Task) -> Result:
        return Result(task_id=task.id, content=self._result_text)


async def test_callback_approve_resolves_gateway():
    reg = AgentRegistry()
    bot = MagicMock(); bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=1, timeout=30)
    reg.set_confirmation_gateway(gw)

    fut = asyncio.get_event_loop().create_future()
    gw._pending["abc"] = fut

    gw._resolve("abc", Decision.APPROVED)
    assert fut.result() is Decision.APPROVED


def test_build_router_accepts_memory():
    from app.bot.handlers import build_router

    class DummyMem:
        def __init__(self): self.cleared = False
        def clear(self): self.cleared = True

    reg = AgentRegistry()
    router = build_router(registry=reg, allowed_id=1, memory=DummyMem())
    assert router is not None
