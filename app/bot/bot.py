from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from app.bot.handlers import build_router
from app.agents.registry import AgentRegistry
from app.config import settings


def create_bot() -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Проверить, что система активна"),
        BotCommand(command="help", description="Справка по возможностям"),
        BotCommand(command="reset", description="Очистить историю диалога"),
    ])


def create_dispatcher(*, registry: AgentRegistry, memory) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(
        registry=registry,
        allowed_id=settings.telegram_user_id,
        memory=memory,
    ))
    return dp
