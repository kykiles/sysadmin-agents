import asyncio
from app.config import settings
from app.logging import setup_logging, get_logger
from app.llm.client import LLMClient
from app.agents.registry import AgentRegistry
from app.agents.director import Director
from app.agents.sysadmin import ChiefSysadmin
from app.bot.bot import create_bot, create_dispatcher
from app.bot.gateway import TelegramConfirmationGateway

log = get_logger("main")


async def main() -> None:
    setup_logging()
    llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    registry = AgentRegistry()
    sysadmin = ChiefSysadmin(llm=llm, registry=registry)
    registry.register(sysadmin)
    director = Director(
        llm=llm, registry=registry,
        available_agents={sysadmin.name: "управление Docker-контейнерами и compose-проектами"},
    )
    registry.register(director)

    bot = create_bot()
    gateway = TelegramConfirmationGateway(bot, chat_id=settings.telegram_user_id)
    registry.set_confirmation_gateway(gateway)

    await registry.run_forever()
    dp = create_dispatcher(registry=registry)

    log.info("startup", model=settings.llm_model, agents=registry.available_agents())
    try:
        await dp.start_polling(bot)
    finally:
        await registry.stop()
        await bot.session.close()
        log.info("shutdown")


if __name__ == "__main__":
    asyncio.run(main())
