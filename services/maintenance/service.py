"""Background maintenance worker for Sib match and queue deadlines."""

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional


from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.config_store import ConfigService
from sib.services.timeout import TimeoutService


# A party younger than this is never swept: players need time to invite friends.
ZOMBIE_PARTY_MIN_AGE = timedelta(hours=2)

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

    INTERVAL_SECONDS = 30

    #: Minimum gap between two reminder sweeps (the DM itself is gated
    #: per player inside ``services/reminders.py``; this only bounds the
    #: candidate query on a 30-second maintenance tick).
    REMINDER_SWEEP_INTERVAL_SECONDS = 600
    _last_reminder_sweep_at: Optional[datetime] = None

    @classmethod
    def _reminder_sweep_due(cls, now: datetime) -> bool:
        last = cls._last_reminder_sweep_at
        if last is None:
            return True
        return (now - last).total_seconds() >= cls.REMINDER_SWEEP_INTERVAL_SECONDS

    #: The weekly league only needs a look once an hour (it finalizes at most
    #: one week, and only after that week has ended).
    LEAGUE_SWEEP_INTERVAL_SECONDS = 3600
    _last_league_sweep_at: Optional[datetime] = None

    @classmethod
    def _league_sweep_due(cls, now: datetime) -> bool:
        last = cls._last_league_sweep_at
        if last is None:
            return True
        return (now - last).total_seconds() >= cls.LEAGUE_SWEEP_INTERVAL_SECONDS

    #: The channel digest is due once an evening; one-minute checks are enough
    #: to catch 20:00 Tehran without querying config on every 30s tick.
    DAILY_DIGEST_SWEEP_INTERVAL_SECONDS = 60
    _last_daily_digest_sweep_at: Optional[datetime] = None
    _last_daily_digest_decision_day: Optional[date] = None

    @classmethod
    def _daily_digest_sweep_due(cls, now: datetime) -> bool:
        last = cls._last_daily_digest_sweep_at
        if last is None:
            return True
        return (now - last).total_seconds() >= cls.DAILY_DIGEST_SWEEP_INTERVAL_SECONDS

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

    @staticmethod
    def _zombie_party_ids(now: datetime):
        """SELECT of the parties whose memberships the sweep may delete.

        A party is swept only when ALL of these hold:

        * it was created more than ``ZOMBIE_PARTY_MIN_AGE`` (2 hours) ago, so a
          brand-new lobby is never touched and players have time to invite
          friends;
        * it is NOT in the matchmaking queue (no active queue entry);
        * it is NOT in an active match. "Active" is the same set of match
          states ``SessionService`` uses (waiting for an opponent, opponent
          found, waiting for acceptance, active), so a party that was matched
          a moment ago and is still accepting is protected as well.
        """
        from sqlalchemy import exists, select
        from sib.database.models import Match, MatchmakingQueue, Party
        from sib.services.session import SessionService

        in_queue = exists(
            select(MatchmakingQueue.id).where(
                MatchmakingQueue.party_id == Party.id,
                MatchmakingQueue.is_active.is_(True),
            )
        )
        in_active_match = exists(
            select(Match.id).where(
                Match.state.in_(SessionService.ACTIVE_MATCH_STATES),
                (Match.party1_id == Party.id) | (Match.party2_id == Party.id),
            )
        )
        return select(Party.id).where(
            Party.created_at < now - ZOMBIE_PARTY_MIN_AGE,
            ~in_queue,
            ~in_active_match,
        )

    @classmethod
    async def cleanup_zombie_parties(cls, session=None, *, now=None) -> int:
        """Delete the memberships of abandoned parties (see ``_zombie_party_ids``).

        Only old parties that are neither queued nor in an active match are
        touched; every other party keeps its members. ``now`` exists for tests.
        """
        from sqlalchemy import delete
        from sib.database.models import PartyMember

        now = now or datetime.now(timezone.utc)
        # One single-table DELETE; the sweep condition is an IN-subquery, so the
        # statement has no extra FROM. (Its predecessor was a multi-table
        # ``DELETE ... USING``: under ORM-enabled DML's default
        # ``synchronize_session="auto"`` it fell back to "fetch", i.e.
        # DELETE..RETURNING, which MariaDB lacks for multi-table deletes, and
        # every cycle died with ``CompileError: Dialect "mysql" does not support
        # RETURNING with DELETE..USING``.) This is still a pure bulk delete - no
        # loaded ORM object needs syncing and the deleted rows are never read
        # back - so session synchronization stays off: no RETURNING, no
        # pre-SELECT. Declared on the statement so BOTH branches below get it.
        stmt = (
            delete(PartyMember)
            .where(PartyMember.party_id.in_(cls._zombie_party_ids(now)))
            .execution_options(synchronize_session=False)
        )
        if session is not None:
            result = await session.execute(stmt)
            count = max(0, result.rowcount or 0)
            if count:
                await session.commit()
        else:
            async with get_session() as own_session:
                result = await own_session.execute(stmt)
                count = max(0, result.rowcount or 0)
                await own_session.commit()
        if count:
            logger.info("Cleaned %s zombie party membership row(s)", count)
        return count

    @classmethod
    async def run_once(cls) -> None:
        """Run one maintenance cycle, then publish any committed fallbacks.

        Failure isolation: EVERY task below runs inside its own
        ``try/except Exception``. A failing task is logged and the next one
        still runs, so one broken task (e.g. the zombie-party sweep) can
        never starve war finalization, timeouts or the invasion tick. Each
        database task also runs in its OWN transaction, so a failure rolls
        back only that task's uncommitted work and never the work another
        task already committed.

        Ordering is part of the bot-turn contract: every DB work of the
        cycle is committed first, bot turns are scheduled IMMEDIATELY after
        the commit, and only then does best-effort Telegram presentation
        run. Presentation awaits (edit retries with flood-wait sleeps) can
        therefore never delay the opening/resumed bot turn of a bot match.
        """
        resolved_ids: list = []
        fallbacks: list[dict] = []
        war_outcomes: list = []

        try:
            await cls.cleanup_zombie_parties()
        except Exception:
            logger.exception("Zombie party cleanup failed")

        try:
            from sib.services.session import SessionService

            async with get_session() as session:
                await SessionService.reconcile_stale_state(session)
        except Exception:
            logger.exception("Maintenance task failed: stale state reconciliation")

        try:
            from sib.services.trading import TradeService

            async with get_session() as session:
                await TradeService.expire_overdue(session)
        except Exception:
            logger.exception("Maintenance task failed: trade expiration")

        try:
            async with get_session() as session:
                await TimeoutService.process_acceptance_timeouts(session)
        except Exception:
            logger.exception("Maintenance task failed: acceptance timeouts")

        try:
            async with get_session() as session:
                resolved_ids = await TimeoutService.process_turn_timeouts(session)
        except Exception:
            resolved_ids = []
            logger.exception("Maintenance task failed: turn timeouts")

        try:
            async with get_session() as session:
                await cls.process_expired_queue_entries(session, fallbacks)
        except Exception:
            # Fallback matches created by the rolled-back transaction do not
            # exist, so they must never be presented.
            fallbacks.clear()
            logger.exception("Maintenance task failed: expired queue entries")

        # War expiration/shields are isolated from matchmaking and Telegram.
        # Winner, loot, 12h peace shield and in_war flags of every due war
        # commit together when this session closes; the result messages are
        # sent only afterwards, in the presentation phase below.
        try:
            from sib.services.war_service import WarService

            async with get_session() as session:
                finished_wars = await WarService.finish_expired_wars(
                    session, outcomes=war_outcomes
                )
            if finished_wars:
                logger.info("Finalized %s expired Apple Clash war(s)", finished_wars)
                for outcome in war_outcomes:
                    logger.info(
                        "Apple Clash war %s settled: winner=%s loser=%s loot=%s",
                        outcome.war_id,
                        outcome.winner_id,
                        outcome.loser_id,
                        outcome.loot,
                    )
        except Exception:
            # The transaction rolled back, so none of these wars is finished:
            # nothing may be announced.
            war_outcomes.clear()
            logger.exception("Apple Clash expiration failed")

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

        # Global Halloween event («سیبومورتیس»): Day-7 weakening, defeat
        # detection, and random group loot chest drops. Gated behind
        # ``halloween_enabled``; DB commits strictly precede bot-turn
        # scheduling and Telegram delivery below.
        halloween_outcome = None
        try:
            from sib.services.halloween import HalloweenService

            if await HalloweenService.is_enabled():
                async with get_session() as session:
                    halloween_outcome = await HalloweenService.run_maintenance_tick(
                        session
                    )
        except Exception:
            halloween_outcome = None
            logger.exception("Halloween maintenance DB tick failed")

        # Marriage & Love DB work (proposal expiration + daily Love decay).
        # It runs as its own isolated transaction, still BEFORE the bot-turn
        # scheduling below, so every DB work of the cycle is committed first
        # - but a Marriage/Love failure can never roll back the matchmaking
        # work committed above, nor skip the bot turns below.
        try:
            await cls.run_marriage_love_cycle()
        except Exception:
            logger.exception("Marriage/Love maintenance cycle failed")

        # Come-back reminders (services/reminders.py): at most one short DM
        # per player per day, only inside the local evening window, only when
        # there is something real to say (chest still closed / fruit ready).
        # Its own guard + its own transactions: a slow Telegram call here can
        # never delay or roll back gameplay work committed above. The sweep
        # itself is throttled - a 30s tick must not scan the user table 2,880
        # times a day for a handful of evening messages.
        try:
            from sib.services.reminders import ReminderService

            sweep_now = datetime.now(timezone.utc)
            if cls._reminder_sweep_due(sweep_now):
                cls._last_reminder_sweep_at = sweep_now
                await ReminderService.run_tick(cls._bot, now=sweep_now)
        except Exception:
            logger.exception("Maintenance task failed: come-back reminders")

        # Weekly group league (services/group_league.py): pay every finished
        # week that is still unsettled (bounded look-back, so an outage never
        # drops a prize), once, and tell the podium's groups. Its own guard,
        # its own session and its own transactions - a league problem can
        # never affect gameplay work. Throttled to once an hour: a settled
        # week costs one primary-key lookup.
        try:
            from sib.services.group_league import GroupLeagueService

            league_now = datetime.now(timezone.utc)
            if cls._league_sweep_due(league_now):
                cls._last_league_sweep_at = league_now
                await GroupLeagueService.finalize_due_weeks(
                    cls._bot, now=league_now
                )
        except Exception:
            logger.exception("Maintenance task failed: group league")

        # V2 is the only production invasion cycle. The feature-flag read is
        # inside the guard too: it must never be able to abort the cycle.
        try:
            if await cls._invasion_enabled():
                # [INVASION-REWORK] guarded — see docs/invasion-rework/
                from sib.services.invasion_v2.scheduler import InvasionScheduler

                terminal = await InvasionScheduler.tick()
                from sib.services.invasion_v2 import admin_runtime
                admin_runtime.last_successful_tick = datetime.now(timezone.utc)
                admin_runtime.last_error = None
                # Presentation never runs in the scheduler or on startup recovery.
                # Terminal IDs come only from a newly committed maintenance tick.
                if cls._bot is not None:
                    from sib.services.invasion_v2.presentation import (
                        InvasionPresentation,
                        close_expired_events,
                        deliver_wither_notifications,
                        publish_applied_events,
                    )
                    from sib.services.invasion_v2.rewards_service import settle_outcome
                    presenter = InvasionPresentation()
                    for inv_id, status in (terminal or []):
                        try:
                            # G2: settle returns the corruption results; DMs go
                            # out only after the transaction has committed.
                            async with get_session() as session:
                                corrupted = await settle_outcome(session, inv_id, status)
                            await deliver_wither_notifications(cls._bot, corrupted)
                            await presenter.finalize(cls._bot, inv_id)
                            # G3: group follow-up for withered gardens (after terminal message, only if any).
                            if corrupted:
                                try:
                                    from sib.services.invasion_v2.presentation import announce_withered_in_group

                                    await announce_withered_in_group(cls._bot, corrupted)
                                except Exception:
                                    logger.exception("inv2 wither group announcement failed invasion=%s", inv_id)
                        except Exception:
                            logger.exception("inv2 terminal settlement failed id=%s", inv_id)
                    await presenter.publish_pending(cls._bot)
                    # IV3 random events: committed by the tick above, delivered
                    # here, and chest buttons are removed once their window is
                    # over. Both passes are best-effort and never raise.
                    await publish_applied_events(cls._bot)
                    await close_expired_events(cls._bot)
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
        if war_outcomes and cls._bot is not None:
            try:
                await cls._announce_war_results(cls._bot, war_outcomes)
            except Exception:
                logger.exception("Could not announce Apple Clash war results")
        if halloween_outcome is not None and cls._bot is not None:
            try:
                from sib.services.halloween import HalloweenService

                await HalloweenService.publish_maintenance_notifications(
                    cls._bot, halloween_outcome
                )
            except Exception:
                logger.exception("Halloween maintenance presentation failed")
        if resolved_ids and cls._bot is not None:
            try:
                await cls._sync_resolved_matches(resolved_ids)
            except Exception:
                logger.exception("Maintenance task failed: resolved match sync")

        # Every source commits level-up state into the XP ledger before this
        # post-transaction publisher runs; this also retries temporary
        # Telegram delivery failures without replaying the source action.
        try:
            from sib.services.player_xp import PlayerXPService

            await PlayerXPService.deliver_pending_notifications(cls._bot)
        except Exception:  # pragma: no cover - background presentation
            logger.exception("Could not deliver pending XP level-up notices")

        # One curated official-channel digest at the configured Tehran hour.
        # This is presentation only, runs after gameplay/XP work, and has its
        # own durable once-per-day claim inside the service.
        try:
            if cls._bot is not None:
                digest_now = datetime.now(timezone.utc)
                from sib.services.daily_channel_digest import (
                    DailyChannelDigestService,
                    TEHRAN as DIGEST_TEHRAN,
                )

                local_digest_day = digest_now.astimezone(DIGEST_TEHRAN).date()
                if (
                    cls._last_daily_digest_decision_day != local_digest_day
                    and cls._daily_digest_sweep_due(digest_now)
                ):
                    cls._last_daily_digest_sweep_at = digest_now
                    result = await DailyChannelDigestService.run_scheduled(
                        cls._bot, now=digest_now
                    )
                    if result.status in {"sent", "skipped", "failed", "sending"}:
                        cls._last_daily_digest_decision_day = result.day
        except Exception:
            logger.exception("Maintenance task failed: daily channel digest")

    @classmethod
    async def _announce_war_results(cls, bot, outcomes: list) -> None:
        """Send the final message of every war settled in this cycle.

        Runs after the settlement committed. Best effort: a failed announcement
        is logged and never undoes a committed settlement, and one war's
        failure never skips the next war (each group is isolated as well).
        """
        from sib.services.war_presentation import announce_war_result

        for outcome in outcomes:
            try:
                await announce_war_result(bot, outcome)
            except Exception:
                logger.exception(
                    "Could not announce Apple Clash war %s result", outcome.war_id
                )

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
        invasion_enabled = await cls._invasion_enabled()
        if invasion_enabled:
            logger.info("Invasion system enabled: True")
        else:
            # The flagship feature ships OFF (migration 036). A silently
            # disabled invasion means even a group whose members all own a
            # Main Card never sees the one thing the /start copy promises,
            # so say it loudly at every boot instead of one INFO line.
            logger.warning(
                "Invasion system DISABLED (system_config.invasion_enabled=false): "
                "no group will ever see a شیاسیب invasion. Turn it on from the "
                "admin chat with /inv2_toggle when you are ready to run it."
            )
        from sib.services.invasion_v2.power_resolver import get_power_resolver

        resolver = get_power_resolver()
        logger.info("inv2 power resolver: %s", type(resolver).__name__ if resolver else "none")

        try:
            while not stop_event.is_set():
                try:
                    await cls.run_once()
                except Exception:
                    # run_once isolates every task itself. This is the last
                    # line of defence: bot.py never awaits this task until
                    # shutdown, so an escaped error would silently kill ALL
                    # maintenance (incl. war finalization) for good.
                    logger.exception("Maintenance cycle crashed; retrying next interval")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=cls.INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("Maintenance worker cancelled")
            raise
        finally:
            logger.info("Maintenance worker stopped")
