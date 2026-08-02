import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.agents.messages import Task, Result, Decision
from app.bot.gateway import TelegramConfirmationGateway


class FakeDirector:
    name = "director"
    def __init__(self, result_text):
        self._result_text = result_text
    async def handle(self, task: Task) -> Result:
        return Result(task_id=task.id, content=self._result_text)


async def test_callback_approve_resolves_gateway():
    bot = MagicMock(); bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=1, timeout=30)

    fut = asyncio.get_event_loop().create_future()
    gw._pending["abc"] = fut

    gw.approve("abc")
    assert fut.result() is Decision.APPROVED


async def test_callback_all_scopes_task_and_approves():
    from app.bot.handlers import build_router
    from unittest.mock import AsyncMock, MagicMock as MM
    bot = MM(); bot.send_message = AsyncMock(return_value=MM(message_id=1))
    gw = TelegramConfirmationGateway(bot, chat_id=1, timeout=30)

    fut = asyncio.get_event_loop().create_future()
    gw._pending["t1"] = fut

    router = build_router(director=FakeDirector("ok"), gateway=gw, allowed_id=1, memory=MM())
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
        def clear(self, chat_id): self.cleared = True

    router = build_router(director=FakeDirector("ok"), allowed_id=1, memory=DummyMem())
    assert router is not None
    names = {h.callback.__name__ for h in router.message.handlers}
    assert {"_start", "_help", "_reset", "_task"} <= names


async def test_set_bot_commands_registers_menu():
    from app.bot.bot import set_bot_commands
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    await set_bot_commands(bot)
    (commands,), _ = bot.set_my_commands.call_args
    assert [c.command for c in commands] == ["start", "help", "reset", "learn", "reload"]


def test_with_quote():
    from app.bot.handlers import with_quote

    def msg(text, *, quote=None, reply=None):
        m = MagicMock()
        m.text = text
        m.quote = MagicMock(text=quote) if quote else None
        m.reply_to_message = MagicMock(text=reply, caption=None) if reply else None
        return m

    assert with_quote(msg("привет")) == "привет"

    out = with_quote(msg("а порт какой?", reply="nginx работает на 80"))
    assert "nginx работает на 80" in out and "а порт какой?" in out

    # выделенный фрагмент важнее всего сообщения
    out = with_quote(msg("почему?", quote="порт 80", reply="nginx работает на 80"))
    assert "порт 80" in out and "nginx" not in out


async def test_report_file_removed_after_send(tmp_path):
    """Отчёт уходит в Telegram и не остаётся на диске."""
    from app.bot.handlers import build_router
    report = tmp_path / "r.md"
    report.write_text("# отчёт", encoding="utf-8")
    director = MagicMock()
    director.handle = AsyncMock(
        return_value=Result(task_id="t", content="итог", attachment=str(report))
    )

    router = build_router(director=director, allowed_id=1, memory=MagicMock())
    handler = [h.callback for h in router.message.handlers if h.callback.__name__ == "_task"][0]

    msg = MagicMock()
    msg.text = "сделай отчёт"
    msg.chat.id = 1
    msg.answer_document = AsyncMock()

    await handler(msg)
    assert msg.answer_document.await_count == 1
    assert not report.exists()
