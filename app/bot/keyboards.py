from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def review_markup(outcome) -> InlineKeyboardMarkup:
    """Кнопки к сводке самопроверки."""
    from app.learning.review import short_id

    return _review_keyboard(
        [(short_id(c.signature), c.label[:24]) for c in outcome.candidates],
        [(short_id(f.scope, f.key), f"{f.scope}/{f.key}"[:24]) for f in outcome.stale],
    )


def _review_keyboard(candidate_ids: list[tuple[str, str]],
                     fact_ids: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"❌ Не предлагать: {label}", callback_data=f"lc:{sid}:no")]
        for sid, label in candidate_ids
    ]
    rows += [
        [InlineKeyboardButton(text=f"🗑 Забыть: {label}", callback_data=f"lf:{sid}:del")]
        for sid, label in fact_ids
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approve_keyboard(task_id: str, tool_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выполнить", callback_data=f"cf:{task_id}:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cf:{task_id}:no"),
        ],
        [
            InlineKeyboardButton(text="✅ Выполнить и больше не спрашивать", callback_data=f"cf:{task_id}:all"),
        ],
    ])
