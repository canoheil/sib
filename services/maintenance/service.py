"""Background maintenance worker for Sib match and queue deadlines."""

import asyncio
import json

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.timeout import TimeoutService


class MaintenanceService:
    """Runs periodic deadline processing and starts timed-out bot matches."""

    INTERVAL_SECONDS = 5
    _bot = None

    @classmethod
    async def process_expired_queue_entries(cls, session, fallback_matches=None) -> int:
        """Turn each expired, still-valid queue into one bot Match.

        The queue and party are re-locked in ``BotMatchService`` before the
        transition. PvP matching uses those same queue locks, which is the
        single-winner guard against a real opponent arriving at timeout.
        ``fallback_matches`` optionally collects presentation references for
        the caller while preserving the historic integer return value.
        """
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sib.database.models import MatchmakingQueue
        from sib.services.bot_match import BotMatchService

        now = datetime.now(timezone.utc)
        stmt = (
            select(MatchmakingQueue.id)
            .where(MatchmakingQueue.is_active == True)
            .where(MatchmakingQueue.expires_at <= now)
            .order_by(MatchmakingQueue.expires_at.asc(), MatchmakingQueue.id.asc())
        )
        entry_ids = list((await session.scalars(stmt)).all())
        processed = 0

        for entry_id in entry_ids:
            match = await BotMatchService.create_fallback_from_queue(
                session, entry_id, now=now
            )
            if match is None:
                continue
            processed += 1
            if fallback_matches is not None:
                fallback_matches.append(
                    {
                        "match_id": match.id,
                        "group_id": match.group1_id,
                        "queue_message_id": (
                            await session.scalar(
                                select(MatchmakingQueue.message_id).where(
                                    MatchmakingQueue.id == entry_id
                                )
                            )
                        ),
                    }
                )

        if processed:
            await session.commit()
            logger.info(f"Started {processed} bot fallback match(es) after matchmaking timeouts")
        return processed

    @classmethod
    async def _present_bot_fallbacks(cls, bot, fallbacks: list[dict]) -> None:
        """Render timeout notice and active board after the DB commit."""
        if bot is None or not fallbacks:
            return

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from sib.database.models import Match, MatchPlayer
        from sib.games.registry import get_game_engine
        from sib.services.game_sync import final_keyboard
        from sib.services.panel import PanelOwnershipService
        from sib.services.telegram_ui import safe_edit_screen, safe_send_screen, with_refresh_button

        for fallback in fallbacks:
            async with get_session() as session:
                match = await session.scalar(
                    select(Match)
                    .where(Match.id == fallback["match_id"])
                    .options(
                        selectinload(Match.game),
                        selectinload(Match.players).selectinload(MatchPlayer.user),
                    )
                )
                if not match or not match.game_state:
                    continue
                state = json.loads(match.game_state)
                engine = get_game_engine(match.game.code)
                human = next((p for p in match.players if p.team == 1 and not p.user.is_bot), None)
                viewer_id = human.user_id if human else 0
                if match.mode == "solo":
                    solo_bot = next(
                        (
                            player for player in match.players
                            if player.user is not None and player.user.is_bot
                        ),
                        None,
                    )
                    bot_name = solo_bot.user.display_name if solo_bot else "حریف سیبی"
                    notice = (
                        "حریفی پیدا نشد.\n\n"
                        f"برای اینکه بیشتر منتظر نمونی، این بار با «{bot_name}» بازی می‌کنی 🍏"
                    )
                else:
                    notice = (
                        "پارتی حریفی پیدا نشد.\n\n"
                        "برای اینکه منتظر نمونید، با تیم «🍎 تیم سیب کوچولو» بازی می‌کنید."
                    )
                board_text = notice + "\n\n" + engine.render_for_group(
                    state, match.group1_id, viewer_id
                )
                keyboard = engine.get_keyboard(state, match.id, viewer_id)
                keyboard = with_refresh_button(keyboard, match.id)
                old_message_id = fallback.get("queue_message_id")
                message_id = None
                if old_message_id:
                    message_id = await safe_edit_screen(
                        bot,
                        match.group1_id,
                        old_message_id,
                        board_text,
                        keyboard,
                    )
                    await PanelOwnershipService.release(session, match.group1_id, old_message_id)
                if message_id is None:
                    sent = await safe_send_screen(
                        bot,
                        match.group1_id,
                        board_text,
                        reply_markup=keyboard,
                    )
                    message_id = getattr(sent, "message_id", None) if sent else None
                if message_id:
                    match.group1_message_id = message_id
                    await PanelOwnershipService.release(session, match.group1_id, message_id)
                    await session.flush()

                after_start = getattr(engine, "after_start", None)
                if after_start:
                    await after_start(bot, match.id)

    @classmethod
    async def _resume_pending_bot_turns(cls, bot) -> None:
        """Self-heal: schedule a bot turn for every live bot match that owns one.

        ``schedule_bot_turn`` is a no-op when a task for the match is already
        in flight (or when the current turn belongs to a human), so this is
        idempotent. It covers tasks lost to a restart and mirrors the
        self-healing contract of the board sync: an active bot match can never
        sit forever on an idle bot's turn.
        """
        from sqlalchemy import select
        from sib.database.models import Match, MatchState
        from sib.bots.runner import schedule_bot_turn

        async with get_session() as session:
            match_ids = list(
                (
                    await session.scalars(
                        select(Match.id).where(
                            Match.is_bot_match == True,  # noqa: E712
                            Match.state == MatchState.ACTIVE,
                        )
                    )
                ).all()
            )

        for match_id in match_ids:
            schedule_bot_turn(match_id, bot=bot)

    @classmethod
    async def run_once(cls) -> None:
        """Run one maintenance cycle, then publish any committed fallbacks."""
        resolved_ids: list = []
        fallbacks: list[dict] = []
        async with get_session() as session:
            try:
                from sib.services.session import SessionService
                from sib.services.trading import TradeService

                await SessionService.reconcile_stale_state(session)
                await TradeService.expire_overdue(session)
                await TimeoutService.process_acceptance_timeouts(session)
                resolved_ids = await TimeoutService.process_turn_timeouts(session)
                await cls.process_expired_queue_entries(session, fallbacks)
            except Exception:
                await session.rollback()
                fallbacks.clear()
                logger.exception("Maintenance cycle failed")

        if fallbacks:
            try:
                await cls._present_bot_fallbacks(cls._bot, fallbacks)
            except Exception:
                logger.exception("Could not render bot fallback match screen")
        if resolved_ids and cls._bot is not None:
            await cls._sync_resolved_matches(resolved_ids)

        # Committed bot matches whose bot turn lost its task (e.g. a restart)
        # are resumed here, after their boards were presented.
        try:
            await cls._resume_pending_bot_turns(cls._bot)
        except Exception:
            logger.exception("Could not resume pending bot turns")

    @classmethod
    async def _sync_resolved_matches(cls, resolved_ids: list) -> None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from sib.database.models import Match, MatchPlayer
        from sib.services.game_sync import sync_match_messages

        async with get_session() as session:
            stmt = (
                select(Match)
                .where(Match.id.in_(list(resolved_ids)))
                .options(
                    selectinload(Match.game),
                    selectinload(Match.players).selectinload(MatchPlayer.user),
                )
            )
            matches = list((await session.scalars(stmt)).all())
            for match in matches:
                await sync_match_messages(
                    cls._bot,
                    match,
                    suffix="⏱ زمان نوبت به پایان رسید.",
                )

    @classmethod
    async def run_forever(cls, stop_event: asyncio.Event, bot=None) -> None:
        """Run maintenance cycles until stop_event is set."""
        cls._bot = bot
        logger.info(f"Maintenance worker started (interval={cls.INTERVAL_SECONDS}s)")

        try:
            while not stop_event.is_set():
                await cls.run_once()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=cls.INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("Maintenance worker cancelled")
            raise
        finally:
            logger.info("Maintenance worker stopped")
