"""Background maintenance worker for Sib match and queue deadlines."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.timeout import TimeoutService


class MaintenanceService:
    """Runs existing periodic database maintenance and timeout recovery."""

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
                refresh_style = "success" if engine.game_code == "battleship" else None
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
    # Invasion scheduling and durable lifecycle (Phase 5)
    # ------------------------------------------------------------------
    # This is a step of the existing maintenance sweep, not a new worker.
    # Occurrence identity and all event timing/configuration live in the DB;
    # this class only asks InvasionService to perform state transitions.
    INVASION_ENEMY_FACTION = "moon_descendants"
    INVASION_WAVE_COUNT = 2
    INVASION_BASE_POWER = 100
    INVASION_MAX_ACTIVITY_BONUS = 100
    INVASION_LEGACY_WAVE_SECONDS = 10 * 60
    MAX_INVASION_EVENTS_PER_CYCLE = 100
    _UTC = timezone.utc

    @classmethod
    def _as_utc(cls, value: datetime) -> datetime:
        """Normalize persisted/test datetimes; SQLite may return naive values."""
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=cls._UTC)
        return value.astimezone(cls._UTC)

    @classmethod
    async def _invasion_timing_config(cls, session) -> tuple[int, int]:
        """Read the two tunable values, falling back safely on invalid config."""
        from sib.services.config_store import CONFIG_DEFAULTS, ConfigService

        interval_default = int(
            CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"]
        )
        duration_default = int(CONFIG_DEFAULTS["invasion_event_duration_seconds"])

        async def configured_int(
            key: str, default: int, minimum: int, maximum: int
        ) -> int:
            try:
                value = await ConfigService.get_int(session, key)
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "Invalid Invasion setting %s; using default %s", key, default
                )
                return default
            if value < minimum or value > maximum:
                logger.warning(
                    "Out-of-range Invasion setting %s=%s; using default %s",
                    key,
                    value,
                    default,
                )
                return default
            return value

        interval = await configured_int(
            "invasion_occurrence_interval_seconds",
            interval_default,
            60,
            31 * 24 * 60 * 60,
        )
        duration = await configured_int(
            "invasion_event_duration_seconds",
            duration_default,
            cls.INVASION_WAVE_COUNT,
            24 * 60 * 60,
        )
        if interval < duration:
            # Avoid overlapping occurrences for one Group if an admin enters
            # a cadence shorter than the complete event duration.
            logger.warning(
                "Invasion cadence (%ss) is shorter than event duration (%ss); "
                "using a safe non-overlapping interval",
                interval,
                duration,
            )
            interval = max(interval_default, duration)
        return interval, duration

    @classmethod
    def _slot_start(
        cls,
        now: datetime,
        interval_seconds: int,
        duration_seconds: int,
    ) -> tuple[int, datetime]:
        """Return the current UTC slot, or the next one if this event expired.

        The polling loop can wake a few seconds after a slot boundary, so the
        current slot remains eligible through its configured event window. If
        that whole window has passed (for example, startup after a long outage),
        choose the next slot rather than manufacturing an already-expired event.
        """
        epoch_seconds = int(cls._as_utc(now).timestamp())
        slot_epoch = (epoch_seconds // interval_seconds) * interval_seconds
        if epoch_seconds >= slot_epoch + duration_seconds:
            slot_epoch += interval_seconds
        return slot_epoch, datetime.fromtimestamp(slot_epoch, tz=cls._UTC)

    @classmethod
    def _occurrence_key(cls, group_id: int, slot_epoch: int) -> str:
        """Stable across retries/restarts; the unique DB constraint is decisive."""
        return (
            f"sib-invasion:v1:{cls.INVASION_ENEMY_FACTION}:"
            f"group:{group_id}:slot:{slot_epoch}"
        )

    @classmethod
    def _enemy_power_for_activity(cls, matches_played: int) -> int:
        """Provisional, bounded activity scaling isolated for future rebalance.

        GroupStats.matches_played is a trustworthy competitive-activity
        counter, but it is not a calibrated combat score. The intentionally
        conservative v1 rule adds at most one base-power's worth of activity;
        the relationship between match history, Group size, and Invasion
        contribution units remains an explicit balancing decision.
        """
        activity = max(0, int(matches_played or 0))
        return cls.INVASION_BASE_POWER + min(
            activity, cls.INVASION_MAX_ACTIVITY_BONUS
        )

    @classmethod
    def _build_invasion_snapshot(
        cls,
        *,
        group_id: int,
        matches_played: int,
        slot_epoch: int,
        scheduled_at: datetime,
        interval_seconds: int,
        duration_seconds: int,
    ) -> tuple[int, dict, dict, list[dict]]:
        """Freeze complete event and two-wave configuration before creation."""
        wave_count = cls.INVASION_WAVE_COUNT
        base_duration, extra_seconds = divmod(duration_seconds, wave_count)
        wave_durations = [
            base_duration + (1 if sequence < extra_seconds else 0)
            for sequence in range(wave_count)
        ]
        elapsed = 0
        wave_deadlines = []
        for wave_duration in wave_durations:
            elapsed += wave_duration
            wave_deadlines.append(
                (scheduled_at + timedelta(seconds=elapsed))
                .astimezone(cls._UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )

        enemy_power = cls._enemy_power_for_activity(matches_played)
        wave_base, remainder = divmod(enemy_power, wave_count)
        wave_compositions = [
            {
                "enemy_faction_code": cls.INVASION_ENEMY_FACTION,
                "wave_sequence": sequence,
                "enemy_power": wave_base + (1 if sequence <= remainder else 0),
            }
            for sequence in range(1, wave_count + 1)
        ]
        event_config = {
            "schema_version": 1,
            "occurrence_slot_epoch": slot_epoch,
            "slot_start_utc": scheduled_at.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "occurrence_interval_seconds": interval_seconds,
            "duration_seconds": duration_seconds,
            "wave_count": wave_count,
            "wave_durations_seconds": wave_durations,
            "wave_deadlines_utc": wave_deadlines,
            "planned_end_at_utc": wave_deadlines[-1],
            "group_matches_played_snapshot": max(0, int(matches_played or 0)),
            "enemy_power_rule": "base_100_plus_matches_played_capped_at_100_v1",
            "enemy_power_base": cls.INVASION_BASE_POWER,
            "enemy_power_activity_bonus_cap": cls.INVASION_MAX_ACTIVITY_BONUS,
            "enemy_power": enemy_power,
        }
        event_spec = {
            "event_type": cls.INVASION_ENEMY_FACTION,
            "group_id": group_id,
            "occurrence_slot_epoch": slot_epoch,
            "wave_count": wave_count,
            "wave_durations_seconds": wave_durations,
            "group_matches_played_snapshot": max(0, int(matches_played or 0)),
        }
        return enemy_power, event_config, event_spec, wave_compositions

    @classmethod
    async def _occurrence_exists(cls, occurrence_key: str) -> bool:
        from sib.database.models import Invasion

        async with get_session() as session:
            return (
                await session.scalar(
                    select(Invasion.id).where(
                        Invasion.occurrence_key == occurrence_key
                    )
                )
            ) is not None

    @classmethod
    async def _schedule_current_invasions(
        cls,
        now: datetime,
        presentation_ids: list[int] | None = None,
    ) -> dict[str, int]:
        """Create at most the current deterministic slot for each active Group.

        Missed historical slots are intentionally not backfilled after a long
        outage. An unexpired current window is eligible; once it has ended, the
        next UTC slot is targeted. The unique key handles retries and competing
        maintenance instances.
        """
        from sib.database.models import Group, GroupStats, Invasion
        from sib.services.invasion import (
            InvasionGroupNotFoundError,
            InvasionOccurrenceConflictError,
            InvasionService,
        )

        stats = {
            "invasions_scheduled": 0,
            "duplicate_invasion_occurrences": 0,
            "invasion_failures": 0,
        }
        async with get_session() as session:
            interval_seconds, duration_seconds = await cls._invasion_timing_config(
                session
            )
            slot_epoch, scheduled_at = cls._slot_start(
                now, interval_seconds, duration_seconds
            )
            group_rows = list(
                (
                    await session.execute(
                        select(
                            Group.id,
                            func.coalesce(GroupStats.matches_played, 0),
                        )
                        .outerjoin(GroupStats, GroupStats.group_id == Group.id)
                        .where(Group.is_active.is_(True))
                        .order_by(Group.id.asc())
                    )
                ).all()
            )

        if not group_rows:
            return stats

        occurrence_keys = [
            cls._occurrence_key(int(group_id), slot_epoch)
            for group_id, _matches_played in group_rows
        ]
        # Keep the IN list below SQLite's traditional parameter limit while
        # avoiding a per-Group transaction when the slot already exists.
        existing_keys = set()
        for offset in range(0, len(occurrence_keys), 500):
            key_batch = occurrence_keys[offset : offset + 500]
            async with get_session() as session:
                existing_keys.update(
                    (
                        await session.scalars(
                            select(Invasion.occurrence_key).where(
                                Invasion.occurrence_key.in_(key_batch)
                            )
                        )
                    ).all()
                )

        for group_id, matches_played in group_rows:
            group_id = int(group_id)
            matches_played = max(0, int(matches_played or 0))
            occurrence_key = cls._occurrence_key(group_id, slot_epoch)
            if occurrence_key in existing_keys:
                stats["duplicate_invasion_occurrences"] += 1
                continue

            enemy_power, event_config, event_spec, wave_compositions = (
                cls._build_invasion_snapshot(
                    group_id=group_id,
                    matches_played=matches_played,
                    slot_epoch=slot_epoch,
                    scheduled_at=scheduled_at,
                    interval_seconds=interval_seconds,
                    duration_seconds=duration_seconds,
                )
            )
            try:
                async with get_session() as session:
                    # Re-check eligibility in the write transaction. A Group
                    # deactivated since the initial scan does not get a new
                    # occurrence; existing Invasions continue independently.
                    active = await session.scalar(
                        select(Group.is_active).where(Group.id == group_id)
                    )
                    if active is not True:
                        continue
                    existing_id = await session.scalar(
                        select(Invasion.id).where(
                            Invasion.occurrence_key == occurrence_key
                        )
                    )
                    if existing_id is not None:
                        stats["duplicate_invasion_occurrences"] += 1
                        continue
                    invasion = await InvasionService.create(
                        session,
                        group_id=group_id,
                        occurrence_key=occurrence_key,
                        scheduled_at=scheduled_at,
                        enemy_faction_code=cls.INVASION_ENEMY_FACTION,
                        enemy_power=enemy_power,
                        event_config=event_config,
                        event_spec=event_spec,
                        wave_compositions=wave_compositions,
                    )
                    invasion_id = invasion.id
                stats["invasions_scheduled"] += 1
                if presentation_ids is not None:
                    presentation_ids.append(int(invasion_id))
                logger.info(
                    "Scheduled Invasion id=%s group_id=%s occurrence_key=%s "
                    "enemy_power=%s",
                    invasion_id,
                    group_id,
                    occurrence_key,
                    enemy_power,
                )
            except (DBAPIError, InvasionOccurrenceConflictError):
                # A concurrent worker may win after our pre-check. Rollback is
                # handled by get_session; the persisted unique key is the final
                # duplicate boundary. Treat a matching key as success/duplicate
                # and never abort unrelated Group scheduling.
                try:
                    if await cls._occurrence_exists(occurrence_key):
                        stats["duplicate_invasion_occurrences"] += 1
                        continue
                except Exception:
                    logger.exception(
                        "Could not verify duplicate Invasion occurrence "
                        "group_id=%s occurrence_key=%s",
                        group_id,
                        occurrence_key,
                    )
                stats["invasion_failures"] += 1
                logger.exception(
                    "Could not schedule Invasion for group_id=%s occurrence_key=%s",
                    group_id,
                    occurrence_key,
                )
            except InvasionGroupNotFoundError:
                stats["invasion_failures"] += 1
                logger.exception(
                    "Group disappeared while scheduling Invasion "
                    "group_id=%s occurrence_key=%s",
                    group_id,
                    occurrence_key,
                )
            except Exception:
                stats["invasion_failures"] += 1
                logger.exception(
                    "Could not schedule Invasion for group_id=%s occurrence_key=%s",
                    group_id,
                    occurrence_key,
                )
        return stats

    @classmethod
    def _deadline_for_wave(cls, invasion, wave) -> datetime:
        """Read a frozen deadline, or derive a stable legacy deadline once."""
        try:
            event_config = json.loads(invasion.event_config_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid persisted Invasion event_config_json") from exc
        if not isinstance(event_config, dict):
            raise ValueError("persisted Invasion event_config_json must be an object")

        sequence = int(wave.wave_sequence)
        deadlines = event_config.get("wave_deadlines_utc")
        if isinstance(deadlines, list) and sequence <= len(deadlines):
            raw_deadline = deadlines[sequence - 1]
            if not isinstance(raw_deadline, str):
                raise ValueError("persisted wave deadline must be an ISO timestamp")
            try:
                return cls._as_utc(
                    datetime.fromisoformat(
                        raw_deadline[:-1] + "+00:00"
                        if raw_deadline.endswith("Z")
                        else raw_deadline
                    )
                )
            except ValueError as exc:
                raise ValueError("invalid persisted wave deadline timestamp") from exc

        anchor = wave.next_transition_at
        if anchor is None:
            raise ValueError("scheduled Invasion wave has no transition timestamp")
        durations = event_config.get("wave_durations_seconds")
        if isinstance(durations, list) and sequence <= len(durations):
            duration = durations[sequence - 1]
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or duration <= 0
            ):
                raise ValueError("persisted wave duration must be a positive integer")
        else:
            # Phase-4 scheduled rows did not yet snapshot wave timing. Freeze a
            # conservative 10-minute per-wave fallback into the wave deadline
            # on its first transition; all Phase-5 occurrences persist an exact
            # per-wave deadline above.
            duration = cls.INVASION_LEGACY_WAVE_SECONDS
        return cls._as_utc(anchor) + timedelta(seconds=duration)

    @classmethod
    async def _due_invasion_candidates(cls, now: datetime) -> list[tuple]:
        from sib.database.models import Invasion, InvasionStatus

        due = or_(
            and_(
                Invasion.status == InvasionStatus.SCHEDULED,
                Invasion.scheduled_at <= now,
            ),
            and_(
                Invasion.status == InvasionStatus.ACTIVE,
                or_(
                    Invasion.next_transition_at <= now,
                    Invasion.current_wave_sequence.is_(None),
                ),
            ),
        )
        async with get_session() as session:
            rows = list(
                (
                    await session.execute(
                        select(
                            Invasion.id,
                            Invasion.group_id,
                            Invasion.occurrence_key,
                            Invasion.status,
                            Invasion.current_wave_sequence,
                        )
                        .where(due)
                        .order_by(
                            Invasion.next_transition_at.asc(), Invasion.id.asc()
                        )
                        .limit(cls.MAX_INVASION_EVENTS_PER_CYCLE)
                    )
                ).all()
            )
        return rows

    @classmethod
    async def _process_due_invasion(
        cls, invasion_id: int, now: datetime
    ) -> str | None:
        from sib.database.models import InvasionStatus
        from sib.services.invasion import InvasionService

        async with get_session() as session:
            invasion = await InvasionService.get(session, invasion_id)
            if invasion.status == InvasionStatus.SCHEDULED:
                if cls._as_utc(invasion.scheduled_at) > now:
                    return None
                wave = await InvasionService.get_current_wave(session, invasion_id)
                if wave is None:
                    raise ValueError("scheduled Invasion has no current wave")
                deadline = cls._deadline_for_wave(invasion, wave)
                await InvasionService.start(
                    session,
                    invasion_id,
                    now=now,
                    deadline_at=deadline,
                    allow_overdue_deadline=True,
                )
                return "started"

            if invasion.status != InvasionStatus.ACTIVE:
                return None
            if invasion.current_wave_sequence is None:
                await InvasionService.resolve(session, invasion_id, now=now)
                return "resolved"

            wave = await InvasionService.get_current_wave(session, invasion_id)
            if wave is None:
                raise ValueError("active Invasion points to a missing current wave")
            if wave.status == InvasionStatus.SCHEDULED:
                if (
                    wave.next_transition_at is None
                    or cls._as_utc(wave.next_transition_at) > now
                ):
                    return None
                deadline = cls._deadline_for_wave(invasion, wave)
                await InvasionService.advance_wave(
                    session,
                    invasion_id,
                    wave.wave_sequence,
                    now=now,
                    deadline_at=deadline,
                    allow_overdue_deadline=True,
                )
                return "wave_started"

            if wave.status == InvasionStatus.ACTIVE:
                if wave.deadline_at is None or cls._as_utc(wave.deadline_at) > now:
                    return None
                next_wave = await InvasionService.advance_wave(
                    session,
                    invasion_id,
                    wave.wave_sequence,
                    now=now,
                )
                if next_wave is None:
                    # A final due wave and its resolution are one bounded
                    # completion operation; no later waves are replayed here.
                    await InvasionService.resolve(session, invasion_id, now=now)
                    return "wave_finished_and_resolved"
                return "wave_finished"

            raise ValueError(
                "unsupported current wave status "
                f"{wave.status!r} for Invasion {invasion_id}"
            )

    @classmethod
    async def run_invasion_cycle(
        cls,
        now: datetime | None = None,
        *,
        schedule_occurrences: bool = True,
        presentation_ids: list[int] | None = None,
    ) -> dict[str, int]:
        """Run one DB-only Invasion step in the existing maintenance sweep.

        Each occurrence and each transition has its own short transaction.
        Processing is one lifecycle transition per Invasion per pass (plus
        final resolution), so a long outage is caught up over later passes
        instead of an unbounded historical loop. No Bot/Telegram object is used.
        """
        now_utc = cls._as_utc(now or datetime.now(cls._UTC))
        stats = {
            "invasions_scheduled": 0,
            "duplicate_invasion_occurrences": 0,
            "invasions_started": 0,
            "invasion_wave_transitions": 0,
            "invasions_resolved": 0,
            "invasion_failures": 0,
        }

        if schedule_occurrences:
            try:
                stats.update(
                    await cls._schedule_current_invasions(
                        now_utc,
                        presentation_ids=presentation_ids,
                    )
                )
            except Exception:
                stats["invasion_failures"] += 1
                logger.exception("Invasion occurrence scheduling scan failed")

        try:
            candidates = await cls._due_invasion_candidates(now_utc)
        except Exception:
            stats["invasion_failures"] += 1
            logger.exception("Invasion due-state scan failed")
            candidates = []

        for invasion_id, group_id, occurrence_key, status, current_wave in candidates:
            try:
                action = await cls._process_due_invasion(int(invasion_id), now_utc)
                if action == "started":
                    stats["invasions_started"] += 1
                elif action in ("wave_started", "wave_finished"):
                    stats["invasion_wave_transitions"] += 1
                elif action == "wave_finished_and_resolved":
                    stats["invasion_wave_transitions"] += 1
                    stats["invasions_resolved"] += 1
                elif action == "resolved":
                    stats["invasions_resolved"] += 1
                if action:
                    if presentation_ids is not None:
                        presentation_ids.append(int(invasion_id))
                    logger.info(
                        "Invasion maintenance action=%s invasion_id=%s group_id=%s "
                        "occurrence_key=%s",
                        action,
                        invasion_id,
                        group_id,
                        occurrence_key,
                    )
            except Exception:
                stats["invasion_failures"] += 1
                logger.exception(
                    "Invasion maintenance failed invasion_id=%s group_id=%s "
                    "occurrence_key=%s status=%s current_wave=%s",
                    invasion_id,
                    group_id,
                    occurrence_key,
                    status,
                    current_wave,
                )

        if any(
            stats[key]
            for key in (
                "invasions_scheduled",
                "invasions_started",
                "invasion_wave_transitions",
                "invasions_resolved",
                "invasion_failures",
            )
        ):
            logger.info("Invasion maintenance: %s", stats)
        return stats

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
        invasion_presentation_ids: list[int] = []
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

        # Durable Invasion scheduling/transitions are another isolated DB-only
        # step of this same loop. The ids are only presentation hints; all event
        # state is committed before the Telegram adapter is called below.
        try:
            await cls.run_invasion_cycle(
                presentation_ids=invasion_presentation_ids,
            )
        except Exception:
            logger.exception("Invasion maintenance cycle failed")

        # Committed bot matches whose bot turn needs a task (a fallback match
        # that just started, or a task lost to a restart) are scheduled here -
        # BEFORE any presentation - so a slow or retrying Telegram edit can
        # never sit between "match is playable" and "bot turn task exists".
        try:
            await cls._resume_pending_bot_turns(cls._bot)
        except Exception:
            logger.exception("Could not resume pending bot turns")

        # Invasion presentation follows the committed domain step and bot-turn
        # scheduling. Pending sends can retry on later maintenance passes; edits
        # are selected only for events whose persisted lifecycle just moved.
        try:
            from sib.services.invasion_telegram import InvasionTelegramService

            await InvasionTelegramService.publish_maintenance_updates(
                cls._bot,
                invasion_presentation_ids,
            )
        except Exception:
            logger.exception("Could not publish Invasion group updates")

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
