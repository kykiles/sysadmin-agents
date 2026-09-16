from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def review_markup(outcome) -> InlineKeyboardMarkup:
    """Кнопки к сводке самопроверки."""
    from app.learning.review import short_id

    rows = [
        [InlineKeyboardButton(text=f"Забыть: {f.scope}/{f.key}"[:28],
                              callback_data=f"lf:{short_id(f.scope, f.key)}:del")]
        for f in outcome.stale
    ]
    # Кнопка карантина несёт id версии предложения, а не ключ: обновлённое под тем
    # же ключом предложение старой кнопкой не одобрить.
    rows += [
        [InlineKeyboardButton(text=f"Принять: {p['scope']}/{p['key']}"[:28],
                              callback_data=f"qf:{p['id']}:ok"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"qf:{p['id']}:no")]
        for p in outcome.tainted
    ]
    rows += [
        [InlineKeyboardButton(text=f"Записать: {f['scope']}/{f['key']}"[:28],
                              callback_data=f"sf:{short_id(f['scope'], f['key'])}:add")]
        for f in outcome.suggested
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approve_keyboard(request_id: str, with_all: bool = False) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="Yes", callback_data=f"cf:{request_id}:yes")]
    if with_all:
        row.append(InlineKeyboardButton(text="Yes to all", callback_data=f"cf:{request_id}:all"))
    row.append(InlineKeyboardButton(text="No", callback_data=f"cf:{request_id}:no"))
    return InlineKeyboardMarkup(inline_keyboard=[row])
