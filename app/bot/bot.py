from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from app.bot.handlers import build_router
from app.agents.registry import AgentRegistry
from app.config import settings


def create_bot() -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


def create_dispatcher(*, registry: AgentRegistry) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(
        registry=registry,
        allowed_id=settings.telegram_user_id,
        chat_id=settings.telegram_user_id,
    ))
    return dp
