from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


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
