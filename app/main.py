import asyncio
from pathlib import Path
from app.config import settings
from app.logging import setup_logging, get_logger
from app.llm.client import LLMClient
from app.agents.director import Director
from app.skills.loader import load_all_skills
from agent_memory.facts import KnowledgeStore
from agent_memory.journal import TaskJournal
from agent_memory.lint import LintState
from app.memory.history import DialogHistory
from app.memory.migrate import migrate_legacy
from app.learning.review import LearningContext
from app.bot.bot import create_bot, create_dispatcher, set_bot_commands
from app.bot.gateway import TelegramConfirmationGateway
from app.bot.progress import TelegramProgress
from app.monitoring.state import MonitorState
from app.monitoring.loop import health_loop, config_from_settings

log = get_logger("main")


async def main() -> None:
    setup_logging()
    llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    # Временные агенты, монитор и консолидация остаются на LLM_MODEL.
    director_llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.director_llm_model,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    ) if settings.director_llm_model else llm
    # Библиотека скилов лежит рядом с пакетом, а не внутри него: ядро не знает,
    # из какой предметной области будут задачи.
    skills_dir = Path(__file__).resolve().parent.parent / "skills"
    learned_dir = Path(settings.learned_skills_dir)
    facts = KnowledgeStore(settings.memory_db_path)
    skills = load_all_skills(skills_dir, learned_dir)
    history = DialogHistory(
        db_path=settings.dialog_db_path,
        limit=settings.dialog_history_limit,
        token_budget=settings.dialog_history_token_budget,
        retention_days=settings.dialog_retention_days,
    )
    journal = TaskJournal(settings.memory_db_path) if settings.journal_enabled else None
    learning = LearningContext(
        facts=facts,
        lint=LintState(settings.memory_db_path),
        llm=llm,
        journal=journal,
    ) if journal is not None else None
    # Хранилища выше создали таблицы — теперь в них есть куда переливать старые базы.
    migrate_legacy(settings.memory_db_path, dialog_db=settings.dialog_db_path)
    bot = create_bot()
    progress = TelegramProgress(bot, settings.telegram_user_id)
    gateway = TelegramConfirmationGateway(bot, chat_id=settings.telegram_user_id, progress=progress)
    director = Director(llm=director_llm, agent_llm=llm, gateway=gateway, memory=history,
                        journal=journal, skills=skills, skills_dir=skills_dir,
                        learned_dir=learned_dir, progress=progress, facts=facts)

    await set_bot_commands(bot)
    dp = create_dispatcher(director=director, gateway=gateway, memory=history,
                           learning=learning, journal=journal)

    monitor_task: asyncio.Task | None = None
    if settings.monitor_enabled:
        state = MonitorState(settings.monitor_db_path)
        monitor_task = asyncio.create_task(
            health_loop(llm, bot, settings.telegram_user_id, state,
                        config_from_settings(), learning)
        )

    log.info("startup", model=settings.llm_model,
             director_model=settings.director_llm_model or settings.llm_model, skills=sorted(skills),
             monitor=settings.monitor_enabled)
    try:
        await dp.start_polling(bot)
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
        await bot.session.close()
        log.info("shutdown")


if __name__ == "__main__":
    asyncio.run(main())
