import asyncio
from pathlib import Path
from app.config import settings
from app.logging import setup_logging, get_logger
from app.llm.client import LLMClient
from app.agents.registry import AgentRegistry
from app.agents.director import Director
from app.agents.loader import load_agents
from app.skills.loader import load_all_skills
from app.memory.history import DialogHistory
from app.memory.facts import init_store, get_store
from app.memory.journal import TaskJournal
from app.learning.detector import CandidateStore
from app.learning.lint import LintState
from app.learning.review import LearningContext
from app.bot.bot import create_bot, create_dispatcher, set_bot_commands
from app.bot.gateway import TelegramConfirmationGateway
from app.monitoring.state import MonitorState
from app.monitoring.loop import health_loop, config_from_settings

log = get_logger("main")


async def main() -> None:
    setup_logging()
    llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    registry = AgentRegistry()
    app_dir = Path(__file__).parent
    init_store(settings.dialog_db_path)
    skills = load_all_skills(app_dir / "skills")
    available = load_agents(app_dir / "agents" / "defs", skills, llm, registry)
    history = DialogHistory(
        db_path=settings.dialog_db_path,
        limit=settings.dialog_history_limit,
        token_budget=settings.dialog_history_token_budget,
        retention_days=settings.dialog_retention_days,
    )
    journal = TaskJournal(settings.journal_db_path) if settings.journal_enabled else None
    learning = LearningContext(
        journal=journal,
        facts=get_store(),
        candidates=CandidateStore(settings.journal_db_path),
        lint=LintState(settings.journal_db_path),
        llm=llm,
    ) if journal is not None else None
    director = Director(llm=llm, registry=registry, available_agents=available,
                        memory=history, journal=journal, skills=skills)
    registry.register(director)

    def reload_library() -> str:
        """Перечитать skills/ и agents/defs/ без рестарта. Новые скиллы и агенты
        подхватываются сразу; изменённый tools.py уже импортированного скилла — нет
        (ponytail: importlib.reload, если понадобится править инструменты на живую)."""
        new_skills = load_all_skills(app_dir / "skills")
        new_agents = load_agents(app_dir / "agents" / "defs", new_skills, llm, registry)
        director.reload_library(new_skills, new_agents)
        return f"навыков: {len(new_skills)}, специалистов: {len(new_agents)}"

    bot = create_bot()
    gateway = TelegramConfirmationGateway(bot, chat_id=settings.telegram_user_id)
    registry.set_confirmation_gateway(gateway)

    await registry.run_forever()
    await set_bot_commands(bot)
    dp = create_dispatcher(registry=registry, memory=history, learning=learning,
                           reload_library=reload_library)

    monitor_task: asyncio.Task | None = None
    if settings.monitor_enabled:
        state = MonitorState(settings.monitor_db_path)
        monitor_task = asyncio.create_task(
            health_loop(llm, bot, settings.telegram_user_id, state,
                        config_from_settings(), learning)
        )

    log.info("startup", model=settings.llm_model, agents=registry.available_agents(),
             monitor=settings.monitor_enabled)
    try:
        await dp.start_polling(bot)
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
        await registry.stop()
        await bot.session.close()
        log.info("shutdown")


if __name__ == "__main__":
    asyncio.run(main())
