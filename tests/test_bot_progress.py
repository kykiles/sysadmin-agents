from unittest.mock import AsyncMock, MagicMock

from app.bot.progress import TelegramProgress

CHAT = 123


def _progress():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    bot.edit_message_text = AsyncMock()
    return TelegramProgress(bot, CHAT), bot


def _last(bot):
    if bot.edit_message_text.await_count:
        return bot.edit_message_text.await_args.args[0]
    return bot.send_message.await_args.args[1]


async def test_plan_is_sent_once_then_edited_in_place():
    p, bot = _progress()
    await p.plan("r", "Пересборка remnabot", ["Найти проект", "Пересобрать"])
    assert _last(bot) == "<b>Пересборка remnabot</b>\n\n⬜ Найти проект\n⬜ Пересобрать"

    await p.started("r", 1, "a#1")
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.await_args.kwargs == {"chat_id": CHAT, "message_id": 7}
    assert "⏳ Найти проект" in _last(bot)

    await p.finished("a#1", success=True)
    assert "✅ Найти проект\n⬜ Пересобрать" in _last(bot)


async def test_waiting_for_confirmation_is_shown_under_step():
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать"])
    await p.started("r", 1, "a#1")
    await p.waiting("a#1", True)
    assert "⏳ Пересобрать\n      <i>ждёт вашего подтверждения</i>" in _last(bot)
    await p.waiting("a#1", False)
    await p.finished("a#1", success=False)
    assert _last(bot).endswith("❌ Пересобрать")


async def test_retry_on_same_step_clears_failure():
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать"])
    await p.started("r", 1, "a#1")
    await p.finished("a#1", success=False)
    await p.started("r", 1, "a#2")
    await p.finished("a#2", success=True)
    assert _last(bot).endswith("✅ Пересобрать")


async def test_replan_keeps_marks_of_unchanged_steps():
    p, bot = _progress()
    await p.plan("r", "t", ["Найти проект", "Пересобрать"])
    await p.started("r", 1, "a#1")
    await p.finished("a#1", success=True)
    await p.plan("r", "t", ["Найти проект", "Пересобрать образ", "Проверить"])
    assert _last(bot).endswith("✅ Найти проект\n⬜ Пересобрать образ\n⬜ Проверить")


async def test_finish_marks_unfinished_work_as_failed_and_forgets_run():
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать", "Проверить"])
    await p.started("r", 1, "a#1")
    await p.finish("r")
    assert _last(bot).endswith("❌ Пересобрать\n⬜ Проверить")
    edits = bot.edit_message_text.await_count
    await p.finished("a#1", success=True)
    assert bot.edit_message_text.await_count == edits


async def test_unknown_step_and_agent_are_ignored():
    p, bot = _progress()
    await p.started("r", 1, "a#1")
    await p.waiting("a#1", True)
    await p.plan("r", "t", ["Один"])
    await p.started("r", 5, "a#2")
    assert bot.send_message.await_count == 1
    bot.edit_message_text.assert_not_awaited()


async def test_html_is_escaped():
    p, bot = _progress()
    await p.plan("r", "<b>", ["a & b"])
    assert _last(bot) == "<b>&lt;b&gt;</b>\n\n⬜ a &amp; b"


async def test_telegram_failure_does_not_break_task():
    p, bot = _progress()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    await p.plan("r", "t", ["Один"])
    await p.started("r", 1, "a#1")
    await p.finish("r")


async def test_refused_confirmation_fails_step_even_if_agent_finished_normally():
    """Живой прогон: после «Нет» агент завершился штатно, и пункт получил ✅."""
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать"])
    await p.started("r", 1, "a#1")
    p.refused("a#1")
    await p.finished("a#1", success=True)
    assert _last(bot).endswith("❌ Пересобрать")
    assert p._refused == set()
