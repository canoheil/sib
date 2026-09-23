"""Main entry point for Goje bot."""

import asyncio
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from goje.core.config import settings
from goje.core.logging import logger, log_event, Events
from goje.database.connection import init_db
from goje.handlers import register_all_handlers


async def create_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    """Create and configure aiogram Bot and Dispatcher instances."""
    if not settings.BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Please provide a valid token in .env")
        sys.exit(1)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()

    # Register handlers
    register_all_handlers(dp)

    return bot, dp


async def main():
    """Bot application lifecycle."""
    logger.info("Initializing Goje bot system...")

    # Initialize database tables and seed data
    await init_db()

    # Perform startup recovery and cleanup
    from goje.database.connection import get_session
    from goje.services.recovery import RecoveryService
    from goje.services.maintenance import MaintenanceService
    async with get_session() as session:
        await RecoveryService.recover_on_startup(session)

    bot, dp = await create_bot_and_dispatcher()

    maintenance_stop = asyncio.Event()
    maintenance_task = asyncio.create_task(
        MaintenanceService.run_forever(maintenance_stop)
    )

    logger.info("Goje bot is ready. Starting polling...")
    log_event(Events.SYSTEM_RESTART_RECOVERY, {"status": "starting_polling"})

    try:
        await dp.start_polling(bot)
    finally:
        maintenance_stop.set()
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()
        logger.info("Goje bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
