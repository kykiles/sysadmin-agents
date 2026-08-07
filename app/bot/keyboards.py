from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def review_markup(outcome) -> InlineKeyboardMarkup:
    """Кнопки к сводке самопроверки."""
    from app.learning.review import short_id

    return _review_keyboard(
        [(short_id(f.scope, f.key), f"{f.scope}/{f.key}"[:24]) for f in outcome.stale],
    )


def _review_keyboard(fact_ids: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Забыть: {label}", callback_data=f"lf:{sid}:del")]
        for sid, label in fact_ids
    ])


def approve_keyboard(task_id: str, tool_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Yes", callback_data=f"cf:{task_id}:yes"),
            InlineKeyboardButton(text="No", callback_data=f"cf:{task_id}:no"),
        ],
        [
            InlineKeyboardButton(text="Yes, and don't ask again", callback_data=f"cf:{task_id}:all"),
        ],
    ])
