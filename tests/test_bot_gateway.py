import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.messages import ConfirmationRequest, Decision
from app.bot.gateway import TelegramConfirmationGateway
from app.bot.render import TELEGRAM_LIMIT

OWNER = 123
MSG_ID = 10


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=MSG_ID))
    bot.send_document = AsyncMock(return_value=MagicMock(message_id=MSG_ID - 1))
    return bot


def _req(tool="docker_restart", **args):
    return ConfirmationRequest(run_id="r1", agent_id="a#1", tool_call_id="c1",
                               tool_name=tool, args=args or {"container": "bot"})


async def _start(gw, req=None):
    """Запустить запрос и дождаться, пока он встанет в ожидание."""
    before = set(gw._pending)
    task = asyncio.create_task(gw.request(req or _req()))
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


def _owner(gw, rid, decision=Decision.APPROVED, **overrides):
    ids = dict(user_id=OWNER, chat_id=OWNER, message_id=MSG_ID) | overrides
    return gw.resolve(rid, decision, **ids)


async def test_owner_button_approves_exact_request():
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=5)
    task, rid = await _start(gw)
    assert _owner(gw, rid)
    assert await task is Decision.APPROVED
    assert gw._pending == {}


async def test_rejected_on_timeout_and_pending_cleared():
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=0)
    assert await gw.request(_req()) is Decision.REJECTED
    assert gw._pending == {}


async def test_stale_button_does_not_approve_next_request_of_same_run():
    """Аудит F04: кнопка истёкшего запроса одобряла следующую операцию той же задачи."""
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=0.01)
    first, old_rid = await _start(gw)
    assert await first is Decision.REJECTED
    gw._timeout = 5
    second, new_rid = await _start(gw, _req("shell_exec", command=["reboot"]))
    assert new_rid != old_rid
    assert not _owner(gw, old_rid)
    assert not second.done()
    assert _owner(gw, new_rid, Decision.REJECTED)
    assert await second is Decision.REJECTED


async def test_duplicate_button_resolves_once():
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=5)
    task, rid = await _start(gw)
    assert _owner(gw, rid)
    assert not _owner(gw, rid)
    assert not _owner(gw, rid, Decision.REJECTED)
    assert await task is Decision.APPROVED


@pytest.mark.parametrize("override", [
    dict(user_id=999), dict(chat_id=-100), dict(message_id=MSG_ID + 1),
])
async def test_button_from_elsewhere_resolves_nothing(override):
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=5)
    task, rid = await _start(gw)
    assert not _owner(gw, rid, **override)
    assert not task.done() and rid in gw._pending
    await _stop(task)
    assert gw._pending == {}


async def test_send_failure_means_rejected_without_pending():
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    assert await gw.request(_req()) is Decision.REJECTED
    assert gw._pending == {}


async def test_parent_cancel_clears_pending():
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=5)
    task, rid = await _start(gw)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gw._pending == {}
    assert not _owner(gw, rid)


async def test_concurrent_requests_are_independent():
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=1), MagicMock(message_id=2)])
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    a, rid_a = await _start(gw, _req("ssh_exec", host="node-a", command=["reboot"]))
    b, rid_b = await _start(gw, _req("ssh_exec", host="node-b", command=["reboot"]))
    # кнопка сообщения a не решает запрос b
    assert not gw.resolve(rid_b, Decision.APPROVED, user_id=OWNER, chat_id=OWNER, message_id=1)
    assert gw.resolve(rid_a, Decision.APPROVED, user_id=OWNER, chat_id=OWNER, message_id=1)
    assert gw.resolve(rid_b, Decision.REJECTED, user_id=OWNER, chat_id=OWNER, message_id=2)
    assert (await a, await b) == (Decision.APPROVED, Decision.REJECTED)


async def test_buttons_carry_only_request_id():
    bot = _bot()
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    task, rid = await _start(gw)
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == [f"cf:{rid}:yes", f"cf:{rid}:all", f"cf:{rid}:no"]
    assert rid in bot.send_message.call_args.args[1]
    await _stop(task)


async def test_long_request_goes_as_file_before_buttons():
    """Аудит F05: хвост длиннее превью исполнялся, хотя человек его не видел."""
    bot = _bot()
    calls = []
    bot.send_document.side_effect = lambda *a, **k: calls.append("document") or MagicMock()
    bot.send_message.side_effect = lambda *a, **k: calls.append("message") or MagicMock(message_id=MSG_ID)
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    command = ["sh", "-c", "x" * 9000 + "; rm -rf /важное"]
    task, rid = await _start(gw, _req("shell_exec", command=command))

    assert calls == ["document", "message"]
    document = bot.send_document.call_args.args[1]
    assert "rm -rf /важное" in document.data.decode("utf-8")
    assert rid in document.filename
    assert len(bot.send_message.call_args.args[1]) <= TELEGRAM_LIMIT
    await _stop(task)


async def test_undelivered_file_means_no_buttons_and_rejection():
    bot = _bot()
    bot.send_document = AsyncMock(side_effect=RuntimeError("file too big"))
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    out = await gw.request(_req("shell_exec", command=["sh", "-c", "x" * 9000]))
    assert out is Decision.REJECTED
    bot.send_message.assert_not_called()
    assert gw._pending == {}


# ---------- «Yes to all»: тот же инструмент и цель, до конца ответа ----------

def _run_req(run="r1", tool="docker_query", **args):
    return ConfirmationRequest(run_id=run, agent_id="a#1", tool_call_id="c1", tool_name=tool,
                               args=args or {"container": "pg", "command": ["psql", "-c", "SELECT 1"]})


async def test_yes_to_all_approves_same_scope_without_asking():
    bot = _bot()
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    task, rid = await _start(gw, _run_req())
    assert _owner(gw, rid, Decision.APPROVED_ALL)
    assert await task is Decision.APPROVED_ALL
    other_query = _run_req(container="pg", command=["psql", "-c", "SELECT 2"])
    assert await gw.request(other_query) is Decision.AUTO_APPROVED
    assert bot.send_message.await_count == 1
    assert "container=pg, program=psql" in bot.send_message.call_args.args[1]


@pytest.mark.parametrize("other", [
    _run_req(container="other", command=["psql", "-c", "SELECT 1"]),
    _run_req(container="pg", command=["mysql", "-e", "SELECT 1"]),
    _run_req(tool="docker_exec", container="pg", command=["psql", "-c", "SELECT 1"]),
    _run_req(run="r2"),
])
async def test_yes_to_all_does_not_cover_other_target_program_tool_or_run(other):
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=5)
    task, rid = await _start(gw, _run_req())
    assert _owner(gw, rid, Decision.APPROVED_ALL)
    await task
    second, _ = await _start(gw, other)
    assert not second.done()
    await _stop(second)


async def test_release_ends_yes_to_all():
    gw = TelegramConfirmationGateway(_bot(), chat_id=OWNER, timeout=5)
    task, rid = await _start(gw, _run_req())
    assert _owner(gw, rid, Decision.APPROVED_ALL)
    await task
    gw.release("r1")
    second, _ = await _start(gw, _run_req())
    assert not second.done()
    await _stop(second)


@pytest.mark.parametrize("req", [
    _run_req(tool="write_skill", name="x", description="d", instructions="i"),
    _run_req(tool="docker_exec", container="pg", command=["sh", "-c", "rm -rf /data"]),
    _run_req(tool="shell_exec", command=["/usr/bin/bash", "-lc", "id"]),
])
async def test_no_yes_to_all_without_recognisable_scope(req):
    bot = _bot()
    gw = TelegramConfirmationGateway(bot, chat_id=OWNER, timeout=5)
    task, rid = await _start(gw, req)
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        f"cf:{rid}:yes", f"cf:{rid}:no"]
    assert not _owner(gw, rid, Decision.APPROVED_ALL)
    assert not task.done()
    assert gw._grants == {}
    await _stop(task)
