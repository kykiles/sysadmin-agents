from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def approve_keyboard(task_id: str, tool_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Approve", callback_data=f"cf:{task_id}:yes"),
            InlineKeyboardButton(text="Reject", callback_data=f"cf:{task_id}:no"),
        ]
    ])
