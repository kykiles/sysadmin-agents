from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def review_markup(outcome) -> InlineKeyboardMarkup:
    """Кнопки к сводке самопроверки."""
    from app.learning.review import short_id

    facts = [(f.scope, f.key) for f in outcome.stale]
    facts += [(f["scope"], f["key"]) for f in outcome.tainted if (f["scope"], f["key"]) not in facts]
    rows = [
        [InlineKeyboardButton(text=f"Забыть: {s}/{k}"[:28], callback_data=f"lf:{short_id(s, k)}:del")]
        for s, k in facts
    ]
    rows += [
        [InlineKeyboardButton(text=f"Записать: {f['scope']}/{f['key']}"[:28],
                              callback_data=f"sf:{short_id(f['scope'], f['key'])}:add")]
        for f in outcome.suggested
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
