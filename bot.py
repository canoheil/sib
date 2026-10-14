"""Main entry point for sib bot."""

import asyncio
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sib.core.config import settings
from sib.core.logging import logger, log_event, Events
from sib.database.connection import init_db
from sib.handlers import register_all_handlers
from sib.middleware.rate_limit import RateLimitMiddleware
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.middleware.group_membership import GroupMembershipMiddleware


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

    # Observe group activity before rate limiting, including dropped updates.
    # Both observers share one throttle; writes run in detached bounded tasks.
    membership = GroupMembershipMiddleware()
    dp.message.outer_middleware(membership)
    dp.callback_query.outer_middleware(membership)

    # Global anti-spam protection. Dropped updates never reach handlers.
    dp.message.outer_middleware(RateLimitMiddleware())
    dp.callback_query.outer_middleware(RateLimitMiddleware())
    dp.callback_query.outer_middleware(PanelGuardMiddleware())

    # Register handlers
    register_all_handlers(dp)

    return bot, dp


async def main():
    """Bot application lifecycle."""
    logger.info("Initializing sib bot system...")

    # Initialize database tables and seed data
    await init_db()

    # Best-effort local artwork-cache cleanup; never enables invasion sends.
    from sib.services.invasion_v2.imaging.cache import prune as prune_inv2_cache
    try:
        prune_inv2_cache()
    except OSError:
        logger.debug("inv2 image cache pruning failed", exc_info=True)

    # Register the authoritative inv2 resolver before recovery/maintenance.
    # This does NOT change the invasion_enabled feature flag.
    from sib.services.invasion_v2.power_resolver import set_power_resolver
    from sib.services.invasion_v2.power_resolver_impl import DbGroupPowerResolver
    set_power_resolver(DbGroupPowerResolver())
    logger.info("inv2 power resolver: DbGroupPowerResolver")

    # Create the bot before recovery so V2 can retry committed, undelivered
    # victory photos. The feature gate still controls all invasion operations.
    bot, dp = await create_bot_and_dispatcher()

    # Perform startup recovery and cleanup
    from sib.database.connection import get_session
    from sib.services.recovery import RecoveryService
    from sib.services.maintenance import MaintenanceService
    async with get_session() as session:
        await RecoveryService.recover_on_startup(session, bot=bot)

    maintenance_stop = asyncio.Event()
    maintenance_task = asyncio.create_task(
        MaintenanceService.run_forever(maintenance_stop, bot=bot)
    )

    logger.info("sib bot is ready. Starting polling...")
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

        # Delayed bot turns and staged Apple Battle narrations are detached by
        # design: cancel the still-sleeping ones so shutdown never leaves a
        # half-run task behind.
        from sib.bots.runner import cancel_all_bot_turns
        from sib.services.apple_battle import AppleBattleService

        await cancel_all_bot_turns()
        await AppleBattleService.cancel_all_narrations()

        await bot.session.close()
        logger.info("sib bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
