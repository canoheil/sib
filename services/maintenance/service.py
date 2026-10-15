"""Background maintenance worker for Sib match and queue deadlines."""

import asyncio
import json
import time
from datetime import datetime, timezone


from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.config_store import ConfigService
from sib.services.timeout import TimeoutService


_INVASION_FLAG_TTL_SECONDS = 60
_invasion_flag_value = False
_invasion_flag_expires_at = 0.0
_invasion_flag_lock = asyncio.Lock()
_invasion_flag_generation = 0


def invalidate_invasion_config_cache() -> None:
    """After a committed admin config write, force the next flag read to DB.

    Generation protects against an overlapping pre-commit read repopulating
    the 60-second cache after this invalidation. In-flight work cannot be
    undone; the next maintenance tick will see the committed value.
    """
    global _invasion_flag_value, _invasion_flag_expires_at, _invasion_flag_generation
    _invasion_flag_generation += 1
    _invasion_flag_value = False
    _invasion_flag_expires_at = 0.0



class MaintenanceService:
    """Runs existing periodic database maintenance and timeout recovery."""

    INTERVAL_SECONDS = 5
    _bot = None

    @classmethod
    async def _invasion_enabled(cls) -> bool:
        """Read `invasion_enabled` from system_config. Cache for 60 seconds.

        Returns True only if the config row exists AND its value is truthy
        ("true", "1", "yes"). Any other value, or a missing row, returns False.
        Database read failures also fail closed for the cache lifetime.
        """
        global _invasion_flag_value, _invasion_flag_expires_at

        if time.monotonic() < _invasion_flag_expires_at:
            return _invasion_flag_value
        async with _invasion_flag_lock:
            generation = _invasion_flag_generation
            if time.monotonic() < _invasion_flag_expires_at:
                return _invasion_flag_value
            try:
                async with get_session() as session:
                    raw = await ConfigService.get_raw(session, "invasion_enabled")
                # ConfigService.set JSON-encodes values; accept both raw DB strings
                # and JSON scalars, but never truth-test an arbitrary nonempty str.
                if raw is not None:
                    try:
                        value = json.loads(raw)
                    except (TypeError, ValueError):
                        value = raw
                    _invasion_flag_value = str(value).strip().lower() in (
                        "true", "1", "yes"
                    )
                else:
                    _invasion_flag_value = False
            except Exception:
                _invasion_flag_value = False
                logger.exception("Could not read invasion_enabled; invasion disabled")
            if generation != _invasion_flag_generation:
                _invasion_flag_value = False
                _invasion_flag_expires_at = 0.0
                return False
            _invasion_flag_expires_at = time.monotonic() + _INVASION_FLAG_TTL_SECONDS
            return _invasion_flag_value

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
        from sib.services.telegram_ui import (
            board_refresh_style,
            safe_edit_screen,
            safe_send_screen,
            with_refresh_button,
        )

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
                refresh_style = board_refresh_style(engine.game_code)
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
                    from sib.services.bot import strip_bot_display_prefix

                    bot_name = (
                        strip_bot_display_prefix(solo_bot.user.display_name)
                        if solo_bot
                        else "حریف سیبی"
                    )
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
                keyboard = with_refresh_button(
                    keyboard, match.id, refresh_style=refresh_style
                )
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

        This must run directly after the committed DB phase - NEVER behind
        Telegram presentation. The opening bot turn of a freshly created
        fallback match is scheduled here, and a retried board edit (which can
        sleep for seconds) must not delay it; the presentation then simply
        renders whatever the authoritative state says at that moment.
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

    # ------------------------------------------------------------------
    # Marriage & Love (Phase 10)
    # ------------------------------------------------------------------
    # Both steps are pure delegation to the Marriage/Love domain services -
    # no expiration or decay logic is reproduced here. Each runs in its OWN
    # transaction so a Marriage/Love failure can neither roll back nor skip
    # unrelated maintenance work (matchmaking timeouts, trade expiry, ...).
    # No new loop is introduced: this is part of the existing ``run_once``
    # sweep, which ``run_forever`` already drives every INTERVAL_SECONDS.

    @classmethod
    async def _expire_marriage_proposals(cls) -> int:
        """Expire overdue ``PENDING`` marriage proposals.

        Delegates to ``MarriageProposalService.expire_overdue_proposals``,
        whose conditional ``UPDATE ... WHERE status = 'PENDING'`` keeps the
        sweep idempotent and safe against a concurrent accept/reject/expire.
        The 5-minute lifetime stays in ``marriage_proposal_timeout_seconds``
        (already stored on each row as ``expires_at``) - nothing is
        hardcoded here. Pending proposals never consumed a Wedding Ring, so
        expiration has nothing to refund.
        """
        from sib.services.marriage_proposal import MarriageProposalService

        try:
            async with get_session() as session:
                expired = await MarriageProposalService.expire_overdue_proposals(session)
                return len(expired)
        except Exception:
            logger.exception("Marriage proposal expiration failed")
            return 0

    @classmethod
    async def _apply_daily_love_decay(cls) -> int:
        """Apply the configured daily Love decay to every ACTIVE marriage.

        Delegates to ``LoveService.apply_daily_decay``, which is idempotent
        per UTC day via ``marriages.last_decay_date``, catches up days
        missed while the bot was offline, and clamps to ``love_min``.
        """
        from sib.services.love import LoveService

        try:
            async with get_session() as session:
                return await LoveService.apply_daily_decay(session)
        except Exception:
            logger.exception("Daily Love decay failed")
            return 0

    @classmethod
    async def _expire_pending_flower_gifts(cls) -> int:
        """Expire PENDING FlowerGifts that outlived their configured lifetime.

        Delegates to ``LoveService.expire_overdue_gifts``, which derives the
        deadline from the existing ``created_at`` column and
        ``flower_gift_pending_timeout_seconds``, refunds exactly one Flower
        per gift, never touches Love, and skips gifts that a concurrent
        accept/reject/invalidate already resolved.
        """
        from sib.services.love import LoveService

        try:
            async with get_session() as session:
                expired = await LoveService.expire_overdue_gifts(session)
                return len(expired)
        except Exception:
            logger.exception("Pending FlowerGift expiration failed")
            return 0

    @classmethod
    async def run_marriage_love_cycle(cls) -> dict:
        """One Marriage/Love maintenance cycle.

        Part of the existing sweep (no second loop). Returns a stats dict;
        each step swallows and logs its own failure, so a Marriage/Love
        problem can never break the rest of the maintenance cycle.
        """
        stats = {
            "expired_marriage_proposals": await cls._expire_marriage_proposals(),
            "love_decay_marriages": await cls._apply_daily_love_decay(),
            "expired_flower_gifts": await cls._expire_pending_flower_gifts(),
        }
        if any(stats.values()):
            logger.info(f"Marriage/Love maintenance: {stats}")
        return stats

    @classmethod
    async def run_once(cls) -> None:
        """Run one maintenance cycle, then publish any committed fallbacks.

        Ordering is part of the bot-turn contract: every DB work of the
        cycle is committed first, bot turns are scheduled IMMEDIATELY after
        the commit, and only then does best-effort Telegram presentation
        run. Presentation awaits (edit retries with flood-wait sleeps) can
        therefore never delay the opening/resumed bot turn of a bot match.
        """
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
                resolved_ids = []
                logger.exception("Maintenance cycle failed")

        # A transient Account XP failure is isolated by a savepoint in match
        # settlement. Retry those committed results before publishing notices.
        try:
            from sib.services.match_result import MatchResultService

            async with get_session() as session:
                retried_xp = await MatchResultService.retry_pending_xp_awards(session)
            if retried_xp:
                logger.info("Retried Account XP for %s completed matches", retried_xp)
        except Exception:  # pragma: no cover - background recovery
            logger.exception("Could not retry pending match XP awards")

        # Marriage & Love DB work (proposal expiration + daily Love decay).
        # It runs as its own isolated transaction, still BEFORE the bot-turn
        # scheduling below, so every DB work of the cycle is committed first
        # - but a Marriage/Love failure can never roll back the matchmaking
        # work committed above, nor skip the bot turns below.
        try:
            await cls.run_marriage_love_cycle()
        except Exception:
            logger.exception("Marriage/Love maintenance cycle failed")

        # V2 is the only production invasion cycle.
        if await cls._invasion_enabled():
            # [INVASION-REWORK] guarded — see docs/invasion-rework/
            try:
                from sib.services.invasion_v2.scheduler import InvasionScheduler

                terminal = await InvasionScheduler.tick()
                from sib.services.invasion_v2 import admin_runtime
                admin_runtime.last_successful_tick = datetime.now(timezone.utc)
                admin_runtime.last_error = None
                # Presentation never runs in the scheduler or on startup recovery.
                # Terminal IDs come only from a newly committed maintenance tick.
                if cls._bot is not None:
                    from sib.services.invasion_v2.presentation import InvasionPresentation
                    from sib.services.invasion_v2.rewards_service import settle_outcome
                    presenter = InvasionPresentation()
                    for inv_id, status in (terminal or []):
                        try:
                            async with get_session() as session:
                                await settle_outcome(session, inv_id, status)
                            await presenter.finalize(cls._bot, inv_id)
                        except Exception:
                            logger.exception("inv2 terminal settlement failed id=%s", inv_id)
                    await presenter.publish_pending(cls._bot)
            except Exception as exc:
                from sib.services.invasion_v2 import admin_runtime
                admin_runtime.last_error = str(exc)[:240]
                logger.exception("Invasion v2 maintenance cycle failed")

        # Committed bot matches whose bot turn needs a task (a fallback match
        # that just started, or a task lost to a restart) are scheduled here -
        # BEFORE any presentation - so a slow or retrying Telegram edit can
        # never sit between "match is playable" and "bot turn task exists".
        try:
            await cls._resume_pending_bot_turns(cls._bot)
        except Exception:
            logger.exception("Could not resume pending bot turns")

        # Direct duels have independent durable deadlines/result/presentation.
        # Isolated transaction: Telegram failures cannot roll back other games.
        try:
            from sib.services.apple_duel_presentation import AppleDuelPresentation

            await AppleDuelPresentation.recover_and_publish(cls._bot)
        except Exception:
            logger.exception("Apple Duel maintenance failed")

        if fallbacks:
            try:
                await cls._present_bot_fallbacks(cls._bot, fallbacks)
            except Exception:
                logger.exception("Could not render bot fallback match screen")
        if resolved_ids and cls._bot is not None:
            await cls._sync_resolved_matches(resolved_ids)

        # Every source commits level-up state into the XP ledger before this
        # post-transaction publisher runs; this also retries temporary
        # Telegram delivery failures without replaying the source action.
        try:
            from sib.services.player_xp import PlayerXPService

            await PlayerXPService.deliver_pending_notifications(cls._bot)
        except Exception:  # pragma: no cover - background presentation
            logger.exception("Could not deliver pending XP level-up notices")

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
                # Phase 2A: an Apple Battle resolved out of band (turn-timeout
                # backstop) still has to drop its temporary narration messages.
                # Best effort - a cleanup failure never affects settlement.
                if getattr(match.game, "code", None) == "apple_battle":
                    try:
                        from sib.services.apple_battle_narration import (
                            AppleBattleNarration,
                        )

                        await AppleBattleNarration.cleanup(cls._bot, match.id)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            f"Could not clean up narration messages for match {match.id}"
                        )

    @classmethod
    async def run_forever(cls, stop_event: asyncio.Event, bot=None) -> None:
        """Run maintenance cycles until stop_event is set."""
        cls._bot = bot
        logger.info(f"Maintenance worker started (interval={cls.INTERVAL_SECONDS}s)")
        logger.info(f"Invasion system enabled: {await cls._invasion_enabled()}")
        from sib.services.invasion_v2.power_resolver import get_power_resolver

        resolver = get_power_resolver()
        logger.info("inv2 power resolver: %s", type(resolver).__name__ if resolver else "none")

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
