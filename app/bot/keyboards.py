from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


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
