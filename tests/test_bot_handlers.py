import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, InaccessibleMessage

from app.agents.messages import ConfirmationRequest, Task, Result, Decision
from app.bot.gateway import TelegramConfirmationGateway

MSG_ID = 10


class FakeDirector:
    name = "director"
    def __init__(self, result_text):
        self._result_text = result_text
    async def handle(self, task: Task) -> Result:
        return Result(task_id=task.id, content=self._result_text)


def test_keyboard_has_only_yes_and_no_for_one_request():
    from app.bot.keyboards import approve_keyboard
    kb = approve_keyboard("r1")
    all_cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all_cbs == ["cf:r1:yes", "cf:r1:no"]


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
    assert [c.command for c in commands] == ["start", "help", "reset", "learn", "reload", "trace"]


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


# ---------- авторизация через router: сообщения и все семейства кнопок (аудит F06) ----------

# Кто нажимает кнопку с корректным callback_data, но права не имеет.
REFUSED = [
    pytest.param(dict(user=999), id="other-user"),
    pytest.param(dict(chat_type="group", chat_id=-100), id="owner-in-group"),
    pytest.param(dict(chat_type="supergroup", chat_id=-100), id="owner-in-supergroup"),
    pytest.param(dict(message="inaccessible"), id="inaccessible-message"),
    pytest.param(dict(message=None), id="no-message"),
]


def _cb(data, *, user=1, chat_type="private", chat_id=1, message="ok", message_id=MSG_ID):
    cb = MagicMock()
    cb.data = data
    cb.from_user.id = user
    cb.answer = AsyncMock()
    if message == "inaccessible":
        cb.message = InaccessibleMessage(chat=Chat(id=chat_id, type=chat_type), message_id=message_id)
    elif message is None:
        cb.message = None
    else:
        cb.message.chat.type = chat_type
        cb.message.chat.id = chat_id
        cb.message.message_id = message_id
        cb.message.html_text = "Подтвердите"
        cb.message.edit_text = AsyncMock()
    return cb


def _router(**kwargs):
    from app.bot.handlers import build_router
    return build_router(director=kwargs.pop("director", FakeDirector("ok")), allowed_id=1,
                        memory=MagicMock(), **kwargs)


def _gateway(timeout=30):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=MSG_ID))
    return TelegramConfirmationGateway(bot, chat_id=1, timeout=timeout)


async def _pending(gw, tool="docker_restart"):
    """Настоящий запрос, вставший в ожидание: (задача, request_id)."""
    before = set(gw._pending)
    task = asyncio.create_task(gw.request(ConfirmationRequest(
        run_id="r1", agent_id="a#1", tool_call_id="c1", tool_name=tool, args={"container": "bot"})))
    for _ in range(100):
        await asyncio.sleep(0)
        if set(gw._pending) - before:
            break
    (rid,) = set(gw._pending) - before
    return task, rid


async def _stop(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _press(router, data, **caller):
    cb = _cb(data, **caller)
    await router.propagate_event(update_type="callback_query", event=cb)
    return cb


@pytest.mark.parametrize("caller", REFUSED)
@pytest.mark.parametrize("choice", ["yes", "no"])
async def test_confirm_callback_refused(caller, choice):
    gw = _gateway()
    task, rid = await _pending(gw)
    await _press(_router(gateway=gw), f"cf:{rid}:{choice}", **caller)
    assert not task.done()
    assert rid in gw._pending
    await _stop(task)


async def test_confirm_callback_owner_in_private_chat():
    gw = _gateway()
    task, rid = await _pending(gw)
    await _press(_router(gateway=gw), f"cf:{rid}:yes")
    assert await task is Decision.APPROVED


def _learning():
    learning = MagicMock()
    learning.pending = {"s1": {"scope": "infra", "key": "k", "value": "v"}}
    return learning


@pytest.mark.parametrize("caller", REFUSED)
async def test_suggested_fact_callback_refused(caller):
    learning = _learning()
    await _press(_router(learning=learning), "sf:s1:add", **caller)
    assert "s1" in learning.pending
    learning.facts.remember.assert_not_called()


async def test_suggested_fact_callback_owner():
    learning = _learning()
    await _press(_router(learning=learning), "sf:s1:add")
    assert learning.pending == {}
    learning.facts.remember.assert_called_once()


@pytest.mark.parametrize("caller", REFUSED)
async def test_forget_fact_callback_refused(caller, monkeypatch):
    import app.bot.handlers as handlers
    monkeypatch.setattr(handlers, "resolve_fact", lambda facts, sid: ("infra", "k"))
    learning = _learning()
    await _press(_router(learning=learning), "lf:s1:del", **caller)
    learning.facts.forget.assert_not_called()


async def test_forget_fact_callback_owner(monkeypatch):
    import app.bot.handlers as handlers
    monkeypatch.setattr(handlers, "resolve_fact", lambda facts, sid: ("infra", "k"))
    learning = _learning()
    await _press(_router(learning=learning), "lf:s1:del")
    learning.facts.forget.assert_called_once_with("infra", "k")


def _msg(*, user=1, chat_type="private", chat_id=1):
    msg = MagicMock()
    msg.text = "проверь диск"
    msg.quote = None
    msg.reply_to_message = None
    msg.from_user.id = user
    msg.chat.type = chat_type
    msg.chat.id = chat_id
    msg.answer = AsyncMock()
    return msg


@pytest.mark.parametrize("sender,reaches", [
    (dict(), True),
    (dict(user=999, chat_id=999), False),
    (dict(chat_type="group", chat_id=-100), False),
])
async def test_task_message_only_from_owner_in_private_chat(sender, reaches):
    director = MagicMock()
    director.handle = AsyncMock(return_value=Result(task_id="t", content="ок"))
    await _router(director=director).propagate_event(
        update_type="message", event=_msg(**sender), bot=MagicMock())
    assert director.handle.await_count == (1 if reaches else 0)


# ---------- кнопка решает ровно один запрос (аудит F04) ----------

async def test_duplicate_press_resolves_once():
    gw = _gateway()
    router = _router(gateway=gw)
    task, rid = await _pending(gw)
    await _press(router, f"cf:{rid}:yes")
    again = await _press(router, f"cf:{rid}:no")
    assert await task is Decision.APPROVED
    assert "устарел" in again.answer.call_args.args[0]
    again.message.edit_text.assert_not_called()


async def test_stale_button_after_timeout_does_not_approve_next_request():
    gw = _gateway(timeout=0.01)
    router = _router(gateway=gw)
    first, old_rid = await _pending(gw)
    assert await first is Decision.REJECTED
    gw._timeout = 30
    second, new_rid = await _pending(gw, tool="shell_exec")
    await _press(router, f"cf:{old_rid}:yes")
    assert not second.done()
    await _press(router, f"cf:{new_rid}:no")
    assert await second is Decision.REJECTED


async def test_button_of_other_message_resolves_nothing():
    gw = _gateway()
    task, rid = await _pending(gw)
    await _press(_router(gateway=gw), f"cf:{rid}:yes", message_id=MSG_ID + 1)
    assert not task.done()
    await _stop(task)


@pytest.mark.parametrize("data", ["cf:{rid}:all", "cf:{rid}", "cf:{rid}:yes:x"])
async def test_old_or_malformed_callback_refused(data):
    gw = _gateway()
    task, rid = await _pending(gw)
    await _press(_router(gateway=gw), data.format(rid=rid))
    assert not task.done()
    await _stop(task)


async def test_edit_failure_does_not_restore_request():
    gw = _gateway()
    router = _router(gateway=gw)
    task, rid = await _pending(gw)
    cb = _cb(f"cf:{rid}:yes")
    cb.message.edit_text = AsyncMock(side_effect=RuntimeError("message is not modified"))
    await router.propagate_event(update_type="callback_query", event=cb)
    assert await task is Decision.APPROVED
    assert gw._pending == {}


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
