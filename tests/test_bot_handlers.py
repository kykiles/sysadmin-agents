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


async def test_callback_all_scopes_task_and_approves():
    from app.bot.handlers import build_router
    from unittest.mock import AsyncMock, MagicMock as MM
    reg = AgentRegistry()
    bot = MM(); bot.send_message = AsyncMock(return_value=MM(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=1, timeout=30)
    reg.set_confirmation_gateway(gw)

    fut = asyncio.get_event_loop().create_future()
    gw._pending["t1"] = fut

    router = build_router(registry=reg, allowed_id=1, memory=MM())
    handler = [h.callback for h in router.callback_query.handlers if h.callback.__name__ == "_confirm"][0]

    cb = MM()
    cb.data = "cf:t1:all"
    cb.answer = AsyncMock()
    cb.message = MM(); cb.message.text = "Подтвердите"; cb.message.edit_text = AsyncMock()

    await handler(cb)
    assert "t1" in gw._scoped
    assert fut.result() is Decision.APPROVED


def test_keyboard_has_allow_all_button():
    from app.bot.keyboards import approve_keyboard
    kb = approve_keyboard("t1", "shell_exec")
    all_cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "cf:t1:yes" in all_cbs
    assert "cf:t1:no" in all_cbs
    assert "cf:t1:all" in all_cbs


def test_build_router_accepts_memory():
    from app.bot.handlers import build_router

    class DummyMem:
        def __init__(self): self.cleared = False
        def clear(self): self.cleared = True

    reg = AgentRegistry()
    router = build_router(registry=reg, allowed_id=1, memory=DummyMem())
    assert router is not None
