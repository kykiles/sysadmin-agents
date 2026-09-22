from unittest.mock import AsyncMock, MagicMock

from app.bot.progress import TelegramProgress

CHAT = 123


def _progress():
    """План в HTML: Rich Message Telegram не принял. Rich-вид — в конце файла."""
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=RuntimeError("rich refused"))
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
    assert _lines(bot) == "<s>1. Найти проект</s>\n<s>2. Пересобрать</s>\n3. Проверить — <i>не понадобился</i>"


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
    assert _lines(bot) == "1. Пересобрать — <i>не выполнено</i>\n2. Проверить — <i>не понадобился</i>"
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


async def test_finish_returns_unfinished_steps_for_the_episode():
    """Ш6: итог доски — вход эпизода задачи."""
    p, bot = _progress()
    await p.plan("r", "t", ["Найти проект", "Пересобрать", "Проверить"])
    await p.started("r", [1, 2, 3], "a#1")
    await p.mark("a#1", 1, "done")
    await p.mark("a#1", 2, "failed")
    await p.finished("a#1")
    assert await p.finish("r") == [
        "пункт «Пересобрать» — не выполнено",
        "пункт «Проверить» — пропущен",
    ]


async def test_finish_without_board_returns_nothing():
    p, _bot = _progress()
    assert await p.finish("r") == []


# ---------- Rich Message: основной вид ----------

def _rich_progress():
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=MagicMock(message_id=7))
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return TelegramProgress(bot, CHAT), bot


def _rich_last(bot):
    if bot.edit_message_text.await_count:
        msg = bot.edit_message_text.await_args.kwargs["rich_message"]
    else:
        msg = bot.send_rich_message.await_args.args[1]
    return msg.model_dump(exclude_none=True)["blocks"]


def _items(bot):
    return [i["blocks"][0]["text"] for i in _rich_last(bot)[1]["items"]]


async def test_rich_plan_sent_once_then_edited_in_place():
    p, bot = _rich_progress()
    await p.plan("r", "Пересборка <b>", ["Найти проект", "Пересобрать"])
    heading, lst = _rich_last(bot)
    assert heading["text"] == "Пересборка <b>" and heading["type"] == "heading"
    assert [(i["value"], i["type"]) for i in lst["items"]] == [(1, "1"), (2, "1")]
    assert _items(bot) == ["Найти проект", "Пересобрать"]

    await p.started("r", [1, 2], "a#1")
    await p.mark("a#1", 1, "done")
    bot.send_message.assert_not_awaited()
    assert bot.send_rich_message.await_count == 1
    kwargs = bot.edit_message_text.await_args.kwargs
    assert (kwargs["chat_id"], kwargs["message_id"], kwargs["parse_mode"]) == (CHAT, 7, None)
    assert _items(bot) == [
        {"type": "strikethrough", "text": "Найти проект"},
        [{"type": "bold", "text": "Пересобрать"}, " — ", {"type": "italic", "text": "в работе"}],
    ]


async def test_rich_other_states_are_plain_text_with_note():
    p, bot = _rich_progress()
    await p.plan("r", "t", ["Пересобрать", "Проверить"])
    await p.started("r", [1, 2], "a#1")
    await p.waiting("a#1", True)
    assert _items(bot)[0] == ["Пересобрать", " — ", {"type": "italic", "text": "ждёт вашего подтверждения"}]
    await p.refused("a#1")
    await p.waiting("a#1", False)
    await p.finished("a#1")
    await p.finish("r")
    assert _items(bot) == [["Пересобрать", " — ", {"type": "italic", "text": "не выполнено"}],
                           ["Проверить", " — ", {"type": "italic", "text": "пропущен"}]]


async def test_refused_rich_falls_back_to_html_for_the_whole_plan():
    p, bot = _progress()
    await p.plan("r", "t", ["Один"])
    await p.started("r", [1], "a#1")
    assert bot.send_rich_message.await_count == 1
    assert "rich_message" not in bot.edit_message_text.await_args.kwargs
    assert _lines(bot) == "1. Один — <i>в работе</i>"


async def test_step_the_agent_dropped_is_not_a_problem_of_the_task():
    """Живой разбор 21.09: пользователя `felia` в панели нет — верный и полный ответ.
    Агент честно отметил «проверить устройства» и «сообщить версию» ненужными, а
    задача из-за этого ушла в журнал как partial."""
    p, _ = _progress()
    await p.plan("r", "t", ["Найти пользователя", "Проверить устройства", "Сообщить версию"])
    await p.started("r", [1, 2, 3], "a#1")
    await p.mark("a#1", 1, "done")
    await p.mark("a#1", 2, "skipped")
    await p.mark("a#1", 3, "skipped")
    await p.finished("a#1")
    assert await p.finish("r") == []


async def test_unmarked_step_is_still_a_problem():
    """Послабление — только на решение агента: пункт, который никто не тронул,
    остаётся сбоем (на проде 16.09 агенты работали, но mark_step не звали)."""
    p, _ = _progress()
    await p.plan("r", "t", ["Пересобрать", "Проверить"])
    await p.started("r", [1, 2], "a#1")
    await p.mark("a#1", 1, "done")
    await p.finished("a#1")
    assert await p.finish("r") == ["пункт «Проверить» — пропущен"]


async def test_failed_step_and_refusal_are_still_problems():
    p, _ = _progress()
    await p.plan("r", "t", ["Перезапустить", "Проверить"])
    await p.started("r", [1, 2], "a#1")
    await p.refused("a#1")
    await p.mark("a#1", 2, "failed")
    await p.finished("a#1")
    assert await p.finish("r") == ["пункт «Перезапустить» — не выполнено",
                                   "пункт «Проверить» — не выполнено"]


async def test_task_broken_mid_step_is_a_problem():
    """Агент не закончил: пункт в работе на момент конца задачи — «не выполнено»."""
    p, _ = _progress()
    await p.plan("r", "t", ["Считать логи"])
    await p.started("r", [1], "a#1")
    assert await p.finish("r") == ["пункт «Считать логи» — не выполнено"]


async def test_step_nobody_was_given_is_not_a_problem():
    """Разбор журнала 22.09 (c6e11258): Директор вписал в план «Собрать итоговый
    отчёт», поручил агенту только пункты 1–2 и отчёт собрал сам — ответ полный,
    а задача ушла в журнал как partial."""
    p, bot = _progress()
    await p.plan("r", "t", ["Считать визиты", "Сравнить по дням", "Собрать итоговый отчёт"])
    await p.started("r", [1, 2], "a#1")
    await p.mark("a#1", 1, "done")
    await p.mark("a#1", 2, "done")
    await p.finished("a#1")
    assert await p.finish("r") == []
    assert _lines(bot).endswith("3. Собрать итоговый отчёт — <i>не понадобился</i>")


async def test_step_given_to_a_finished_agent_stays_assigned():
    """Пункт поручали, агент ушёл, не отметив, — «пропущен», даже если потом
    другой агент получил другие пункты."""
    p, _ = _progress()
    await p.plan("r", "t", ["Один", "Два"])
    await p.started("r", [1], "a#1")
    await p.finished("a#1")
    await p.started("r", [2], "a#2")
    await p.mark("a#2", 2, "done")
    await p.finished("a#2")
    assert await p.finish("r") == ["пункт «Один» — пропущен"]
