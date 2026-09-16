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


def _lines(bot):
    return _last(bot).split("\n\n", 1)[1]


async def test_plan_is_sent_once_then_edited_in_place():
    p, bot = _progress()
    await p.plan("r", "Пересборка remnabot", ["Найти проект", "Пересобрать"])
    assert _last(bot) == "<b>Пересборка remnabot</b>\n\n1. Найти проект\n2. Пересобрать"
    assert p.steps("r") == ["Найти проект", "Пересобрать"]

    await p.started("r", [1, 2], "a#1")
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.await_args.kwargs == {"chat_id": CHAT, "message_id": 7}
    assert _lines(bot) == "1. Найти проект — <i>в работе</i>\n2. Пересобрать — <i>в работе</i>"


async def test_agent_marks_its_steps_one_by_one():
    p, bot = _progress()
    await p.plan("r", "t", ["Найти проект", "Пересобрать", "Проверить"])
    await p.started("r", [1, 2, 3], "a#1")
    assert await p.mark("a#1", 1, "done") == {"marked": 1, "status": "done"}
    assert _lines(bot) == "<s>1. Найти проект</s>\n2. Пересобрать — <i>в работе</i>\n3. Проверить — <i>в работе</i>"
    await p.mark("a#1", 2, "done")
    await p.mark("a#1", 3, "skipped")
    await p.finished("a#1")
    assert _lines(bot) == "<s>1. Найти проект</s>\n<s>2. Пересобрать</s>\n3. Проверить — <i>пропущен</i>"


async def test_agent_cannot_mark_foreign_step():
    p, bot = _progress()
    await p.plan("r", "t", ["Один", "Два"])
    await p.started("r", [1], "a#1")
    assert "не твой" in (await p.mark("a#1", 2, "done"))["error"]
    assert "не твой" in (await p.mark("a#2", 1, "done"))["error"]
    assert _lines(bot) == "1. Один — <i>в работе</i>\n2. Два"


async def test_finishing_agent_does_not_mark_steps_done():
    """Автоматическая отметка «выполнен» за «агент закончил» врала после отказа."""
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать"])
    await p.started("r", [1], "a#1")
    await p.finished("a#1")
    assert _lines(bot) == "1. Пересобрать"
    await p.finish("r")
    assert _lines(bot) == "1. Пересобрать — <i>пропущен</i>"


async def test_waiting_and_refusal_land_on_current_step():
    """Живой прогон: агент осмотрел проект, а пересборку пользователь отклонил —
    «не выполнено» должен получить только пункт пересборки."""
    p, bot = _progress()
    await p.plan("r", "t", ["Найти проект", "Пересобрать", "Проверить"])
    await p.started("r", [1, 2, 3], "a#1")
    await p.mark("a#1", 1, "done")
    await p.waiting("a#1", True)
    assert _lines(bot) == ("<s>1. Найти проект</s>\n2. Пересобрать — <i>ждёт вашего подтверждения</i>\n"
                           "3. Проверить — <i>в работе</i>")
    await p.waiting("a#1", False)
    await p.refused("a#1")
    assert "отметь failed" in (await p.mark("a#1", 2, "done"))["error"]
    await p.finished("a#1")
    await p.finish("r")
    assert _lines(bot) == "<s>1. Найти проект</s>\n2. Пересобрать — <i>не выполнено</i>\n3. Проверить — <i>пропущен</i>"
    assert p._refused == set() and p._agents == {}


async def test_new_agent_may_redo_refused_step():
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать"])
    await p.started("r", [1], "a#1")
    await p.refused("a#1")
    await p.finished("a#1")
    await p.started("r", [1], "a#2")
    await p.mark("a#2", 1, "done")
    assert _lines(bot) == "<s>1. Пересобрать</s>"


async def test_replan_keeps_marks_of_unchanged_steps():
    p, bot = _progress()
    await p.plan("r", "t", ["Найти проект", "Пересобрать"])
    await p.started("r", [1], "a#1")
    await p.mark("a#1", 1, "done")
    await p.finished("a#1")
    await p.plan("r", "t", ["Найти проект", "Пересобрать образ", "Проверить"])
    assert _lines(bot) == "<s>1. Найти проект</s>\n2. Пересобрать образ\n3. Проверить"


async def test_finish_fails_steps_with_running_agent():
    p, bot = _progress()
    await p.plan("r", "t", ["Пересобрать", "Проверить"])
    await p.started("r", [1], "a#1")
    await p.finish("r")
    assert _lines(bot) == "1. Пересобрать — <i>не выполнено</i>\n2. Проверить — <i>пропущен</i>"
    edits = bot.edit_message_text.await_count
    await p.finished("a#1")
    assert bot.edit_message_text.await_count == edits
    assert p.steps("r") is None


async def test_unknown_agent_is_ignored():
    p, bot = _progress()
    await p.waiting("a#1", True)
    await p.refused("a#1")
    await p.finished("a#1")
    bot.send_message.assert_not_awaited()


async def test_html_is_escaped():
    p, bot = _progress()
    await p.plan("r", "<b>", ["a & b"])
    assert _last(bot) == "<b>&lt;b&gt;</b>\n\n1. a &amp; b"


async def test_telegram_failure_does_not_break_task():
    p, bot = _progress()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    await p.plan("r", "t", ["Один"])
    await p.started("r", [1], "a#1")
    await p.finish("r")
