"""Background maintenance worker for Sib match and queue deadlines."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.config_store import ConfigService
from sib.services.invasion import LEGACY_WAVE_COUNT, MAX_SIGNED_INTEGER
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



@dataclass(frozen=True)
class InvasionBattleConfig:
    """Validated battle configuration read once per scheduling pass."""

    interval_seconds: int
    wave_count: int
    wave_seconds: int
    enemy_base_power: int
    enemy_activity_cap: int
    max_compensation_seconds: int


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
    # Invasion scheduling and durable lifecycle
    # ------------------------------------------------------------------
    # This is a step of the existing maintenance sweep, not a new worker.
    # Occurrence identity lives in the DB. Four-wave battles activate inside
    # their slot's first battle window and run on an activation-relative
    # timeline (InvasionService.advance_battle). Legacy Phase-5 events keep
    # their original two-wave timeline; they are never reinterpreted.
    INVASION_ENEMY_FACTION = "moon_descendants"
    # Legacy (Phase-5) constants, kept for legacy events and the balance
    # analysis that documents them. Four-wave battles read configuration.
    INVASION_LEGACY_WAVE_COUNT = 2
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
    async def _battle_config(cls, session) -> "InvasionBattleConfig":
        """Read and validate the battle configuration, falling back safely.

        Each value is range-checked; an invalid value logs a warning and uses
        its default. The battle window must fit twice inside one cadence slot,
        because a battle may activate as late as the end of its window and run
        a full window longer. If the configured pair violates that, the default
        pair is used; the cadence is widened only if even the defaults do not fit.
        """
        from sib.services.config_store import CONFIG_DEFAULTS, ConfigService

        async def configured_int(key: str, minimum: int, maximum: int) -> int:
            default = int(CONFIG_DEFAULTS[key])
            try:
                value = await ConfigService.get_int(session, key)
            except (TypeError, ValueError, OverflowError):
                logger.warning("Invalid Invasion setting %s; using default %s", key, default)
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
            "invasion_occurrence_interval_seconds", 60, 31 * 24 * 60 * 60
        )
        wave_count = await configured_int(
            "invasion_wave_count", 1, MAX_SIGNED_INTEGER
        )
        wave_seconds = await configured_int(
            "invasion_wave_duration_seconds", 60, interval
        )
        if 2 * wave_count * wave_seconds > interval:
            logger.warning(
                "Invasion battle window (%ss x2) does not fit the %ss cadence; "
                "using default wave configuration",
                wave_count * wave_seconds,
                interval,
            )
            wave_count = int(CONFIG_DEFAULTS["invasion_wave_count"])
            wave_seconds = int(CONFIG_DEFAULTS["invasion_wave_duration_seconds"])
            if 2 * wave_count * wave_seconds > interval:
                interval = max(interval, 2 * wave_count * wave_seconds)

        base_power = await configured_int(
            "invasion_enemy_base_power", 1, MAX_SIGNED_INTEGER
        )
        activity_cap = await configured_int(
            "invasion_enemy_activity_bonus_cap", 0, MAX_SIGNED_INTEGER
        )
        max_compensation = await configured_int(
            "invasion_downtime_compensation_max_seconds",
            0,
            31 * 24 * 60 * 60,
        )
        return InvasionBattleConfig(
            interval_seconds=interval,
            wave_count=wave_count,
            wave_seconds=wave_seconds,
            enemy_base_power=base_power,
            enemy_activity_cap=activity_cap,
            max_compensation_seconds=max_compensation,
        )

    @staticmethod
    def _eligible_slot(
        now: datetime,
        interval_seconds: int,
        window_seconds: int,
    ) -> Optional[tuple[int, datetime]]:
        """Return the UTC slot that may activate now, or None.

        Boundary policy (half-open window): a slot at epoch ``S`` may activate at
        any instant ``t`` with ``S <= t < S + window``. At exactly ``S`` the
        battle activates; at exactly ``S + window`` the slot is already closed.
        Outside every window nothing is created, so no future battle is ever
        pre-created or announced ahead of time. Missed slots are not backfilled.
        """
        ts = MaintenanceService._as_utc(now).timestamp()
        slot_epoch = (int(ts) // interval_seconds) * interval_seconds
        if slot_epoch <= ts < slot_epoch + window_seconds:
            return slot_epoch, datetime.fromtimestamp(slot_epoch, tz=MaintenanceService._UTC)
        return None

    @classmethod
    def _occurrence_key(cls, group_id: int, slot_epoch: int) -> str:
        """Stable across retries/restarts; the unique DB constraint is decisive."""
        return (
            f"sib-invasion:v1:{cls.INVASION_ENEMY_FACTION}:"
            f"group:{group_id}:slot:{slot_epoch}"
        )

    @classmethod
    def _enemy_power_for_activity(cls, matches_played: int) -> int:
        """Legacy (Phase-5) rule for two-wave events, kept for compatibility."""
        activity = max(0, int(matches_played or 0))
        return cls.INVASION_BASE_POWER + min(
            activity, cls.INVASION_MAX_ACTIVITY_BONUS
        )

    @classmethod
    def _battle_enemy_power(cls, cfg: "InvasionBattleConfig", matches_played: int) -> int:
        """Enemy strength for a four-wave battle.

        ``(base + min(matches, cap)) * wave_count / 2``: each wave keeps the
        strength of a legacy wave, so the defense-to-enemy ratio, and therefore
        the outcome curve for a given participation, is unchanged by adding
        waves. Integer floor division.
        """
        activity = max(0, int(matches_played or 0))
        scaled_base = cfg.enemy_base_power + min(activity, cfg.enemy_activity_cap)
        return scaled_base * cfg.wave_count // LEGACY_WAVE_COUNT

    @classmethod
    def _build_battle_snapshot(
        cls,
        *,
        group_id: int,
        matches_played: int,
        slot_epoch: int,
        scheduled_at: datetime,
        activated_at: datetime,
        cfg: "InvasionBattleConfig",
    ) -> tuple[int, dict, dict, list[dict]]:
        """Freeze the four-wave event configuration at activation.

        Wave deadlines are deliberately NOT stored here: they are derived from
        the persisted activation time and the wave duration, so one definition
        governs every boundary and nothing can drift from the activation instant.
        """
        enemy_power = cls._battle_enemy_power(cfg, matches_played)
        wave_base, remainder = divmod(enemy_power, cfg.wave_count)
        wave_compositions = [
            {
                "enemy_faction_code": cls.INVASION_ENEMY_FACTION,
                "wave_sequence": sequence,
                "enemy_power": wave_base + (1 if sequence <= remainder else 0),
            }
            for sequence in range(1, cfg.wave_count + 1)
        ]
        activity = max(0, int(matches_played or 0))
        event_config = {
            "schema_version": 2,
            "timing_basis": "activation",
            "occurrence_slot_epoch": slot_epoch,
            "slot_start_utc": scheduled_at.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "activated_at_utc": activated_at.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "occurrence_interval_seconds": cfg.interval_seconds,
            "battle_window_seconds": cfg.wave_count * cfg.wave_seconds,
            "wave_count": cfg.wave_count,
            "wave_duration_seconds": cfg.wave_seconds,
            "group_matches_played_snapshot": activity,
            "enemy_power_rule": "scaled_base_plus_matches_capped_v2",
            "enemy_power_base": cfg.enemy_base_power,
            "enemy_power_activity_bonus_cap": cfg.enemy_activity_cap,
            "enemy_power": enemy_power,
            # Total heartbeat downtime this battle may be compensated for.
            "downtime_compensation_max_seconds": cfg.max_compensation_seconds,
            # Settlement pays only persisted defenders (see InvasionRewardService).
            "reward_rule": "defender",
        }
        event_spec = {
            "event_type": cls.INVASION_ENEMY_FACTION,
            "group_id": group_id,
            "occurrence_slot_epoch": slot_epoch,
            "wave_count": cfg.wave_count,
            "wave_duration_seconds": cfg.wave_seconds,
            "group_matches_played_snapshot": activity,
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
        """Activate the current slot's battle for each active Group, if eligible.

        A battle is created already ACTIVE, at the tick that first observes its
        slot inside the battle window, and is announced only then. A Group that
        still has an unfinished battle is skipped, so two battles of one Group
        can never overlap; the Group row lock serializes that check per Group.
        """
        from sib.database.models import Group, GroupStats, Invasion, InvasionStatus
        from sib.services.factions import FactionService
        from sib.services.invasion import (
            InvasionGroupNotFoundError,
            InvasionOccurrenceConflictError,
            InvasionService,
        )

        stats = {
            "invasions_scheduled": 0,
            "duplicate_invasion_occurrences": 0,
            "invasions_skipped_overlap": 0,
            "invasion_failures": 0,
        }
        async with get_session() as session:
            cfg = await cls._battle_config(session)
            # Load the invading Faction once per scheduling pass so we can
            # snapshot its home territory as the battle's target region.
            # Region metadata is optional on the Invasion row; if the faction
            # or its territory is missing we simply leave the fields NULL and
            # the UI falls back to "مشخص نشده" (preserves the pre-fix shape).
            enemy_faction = await FactionService.get_by_code(
                session, cls.INVASION_ENEMY_FACTION
            )
            target_region_name: Optional[str] = None
            if enemy_faction is not None and enemy_faction.territory_fa:
                target_region_name = enemy_faction.territory_fa
        eligible = cls._eligible_slot(now, cfg.interval_seconds, cfg.wave_count * cfg.wave_seconds)
        if eligible is None:
            # Outside every slot's battle window: nothing to create, nothing
            # to announce, and no future slot is pre-created.
            return stats
        slot_epoch, scheduled_at = eligible
        activated_at = cls._as_utc(now)

        async with get_session() as session:
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
                cls._build_battle_snapshot(
                    group_id=group_id,
                    matches_played=matches_played,
                    slot_epoch=slot_epoch,
                    scheduled_at=scheduled_at,
                    activated_at=activated_at,
                    cfg=cfg,
                )
            )
            try:
                async with get_session() as session:
                    # Serialize scheduling per Group, then re-check everything the
                    # decision depends on: active flag, duplicate key, and any
                    # unfinished battle of this Group.
                    active = await session.scalar(
                        select(Group.is_active)
                        .where(Group.id == group_id)
                        .with_for_update()
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
                    unfinished_id = await session.scalar(
                        select(Invasion.id)
                        .where(
                            Invasion.group_id == group_id,
                            Invasion.status != InvasionStatus.FINISHED,
                        )
                        .limit(1)
                    )
                    if unfinished_id is not None:
                        stats["invasions_skipped_overlap"] += 1
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
                        activated_at=activated_at,
                        wave_duration_seconds=cfg.wave_seconds,
                        # Snapshot the invading Faction's home territory as the
                        # battle's target region when the faction metadata is
                        # present. The field is optional; NULL values preserve
                        # the "مشخص نشده" rendering used by older records.
                        target_region_name_snapshot=target_region_name,
                    )
                    invasion_id = invasion.id
                stats["invasions_scheduled"] += 1
                if presentation_ids is not None:
                    presentation_ids.append(int(invasion_id))
                logger.info(
                    "Activated Invasion id=%s group_id=%s occurrence_key=%s "
                    "enemy_power=%s waves=%s wave_seconds=%s",
                    invasion_id,
                    group_id,
                    occurrence_key,
                    enemy_power,
                    cfg.wave_count,
                    cfg.wave_seconds,
                )
            except (DBAPIError, InvasionOccurrenceConflictError):
                # A concurrent worker may win after our pre-check. Rollback is
                # handled by get_session; the persisted unique key is the final
                # duplicate boundary.
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
        """Legacy (two-wave) deadline: frozen snapshot or stable legacy fallback."""
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
            duration = cls.INVASION_LEGACY_WAVE_SECONDS
        return cls._as_utc(anchor) + timedelta(seconds=duration)

    @classmethod
    async def _candidate_invasions(cls, now: datetime) -> list[tuple]:
        """Every ACTIVE battle (heartbeat refresh) plus legacy due SCHEDULED rows.

        Active battles are always visited, not only when a boundary is due,
        because each visit refreshes the heartbeat that outage restoration
        depends on. Stalest heartbeats come first.
        """
        from sib.database.models import Invasion, InvasionStatus

        due = or_(
            and_(
                Invasion.status == InvasionStatus.SCHEDULED,
                Invasion.scheduled_at <= now,
            ),
            Invasion.status == InvasionStatus.ACTIVE,
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
                            Invasion.heartbeat_at.asc(),
                            Invasion.next_transition_at.asc(),
                            Invasion.id.asc(),
                        )
                        .limit(cls.MAX_INVASION_EVENTS_PER_CYCLE)
                    )
                ).all()
            )
        return rows

    @classmethod
    async def _process_invasion(cls, invasion_id: int, now: datetime) -> list[str]:
        """Advance one Invasion; return the committed action tokens."""
        from sib.database.models import InvasionStatus
        from sib.services.invasion import InvasionService

        async with get_session() as session:
            invasion = await InvasionService.get(session, invasion_id)
            if (
                invasion.status == InvasionStatus.ACTIVE
                and InvasionService._battle_timing(invasion) is not None
            ):
                result = await InvasionService.advance_battle(
                    session, invasion_id, now=now
                )
                actions: list[str] = []
                if result.paused_seconds:
                    actions.append("restored")
                for item in result.transitions:
                    if item == "interrupted":
                        actions.append("interrupted")
                    elif item == "resolved":
                        actions.append("resolved")
                    else:
                        actions.append("wave_started")
                return actions

        action = await cls._process_legacy_invasion(invasion_id, now)
        return [action] if action else []

    @classmethod
    async def _process_legacy_invasion(cls, invasion_id: int, now: datetime) -> str | None:
        """Original two-wave timeline, unchanged for pre-existing events."""
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
                if now >= deadline:
                    # The whole first window passed before anyone could join (for
                    # example after an outage). It is closed without a defeat.
                    voided = await InvasionService.void_missed_legacy_battle(
                        session, invasion_id, now=now
                    )
                    return "voided" if voided else None
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
    async def _settle_pending_rewards(cls, now: datetime) -> tuple[int, int]:
        """Pay each finished, unsettled Invasion once; return (settled, failed).

        Every event is settled in its own transaction. The settled marker is
        written with the Golden Apple item grants, so a failed grant leaves the
        event pending and it is retried on a later pass. The marker check under
        the Invasion lock makes a repeat after a crash a no-op. Only events with
        an approved outcome qualify, so interrupted (outcome NULL) battles are
        never paid.
        """
        from sib.database.models import Invasion, InvasionStatus, InvasionOutcome
        from sib.services.invasion_rewards import InvasionRewardService

        async with get_session() as session:
            ids = list(
                (
                    await session.scalars(
                        select(Invasion.id)
                        .where(
                            Invasion.status == InvasionStatus.FINISHED,
                            Invasion.outcome.in_(InvasionOutcome.ALL),
                            Invasion.rewards_settled_at.is_(None),
                        )
                        .order_by(Invasion.ended_at.asc(), Invasion.id.asc())
                        .limit(cls.MAX_INVASION_EVENTS_PER_CYCLE)
                    )
                ).all()
            )
        settled = 0
        failed = 0
        for invasion_id in ids:
            try:
                async with get_session() as session:
                    await InvasionRewardService.settle_completed_invasion(
                        session,
                        int(invasion_id),
                        now=now,
                    )
                settled += 1
            except Exception:
                failed += 1
                logger.exception(
                    "Invasion reward settlement failed; will retry invasion_id=%s",
                    invasion_id,
                )
        return settled, failed

    @classmethod
    async def run_invasion_cycle(
        cls,
        now: datetime | None = None,
        *,
        schedule_occurrences: bool = True,
        presentation_ids: list[int] | None = None,
    ) -> dict[str, int]:
        """Run one DB-only Invasion step in the existing maintenance sweep.

        Order: activate the current slot's battle (if eligible) -> advance every
        ACTIVE battle and any due legacy row -> settle finished battles. Each
        Invasion has its own short transaction. No Bot/Telegram object is used.
        """
        now_utc = cls._as_utc(now or datetime.now(cls._UTC))
        stats = {
            "invasions_scheduled": 0,
            "duplicate_invasion_occurrences": 0,
            "invasions_skipped_overlap": 0,
            "invasions_started": 0,
            "invasions_voided": 0,
            "invasion_heartbeat_restores": 0,
            "invasion_wave_transitions": 0,
            "invasions_resolved": 0,
            "invasions_interrupted": 0,
            "invasion_rewards_settled": 0,
            "invasion_failures": 0,
        }

        if schedule_occurrences and await cls._invasion_enabled():
            # [INVASION-REWORK] guarded — see docs/invasion-rework/
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
            candidates = await cls._candidate_invasions(now_utc)
        except Exception:
            stats["invasion_failures"] += 1
            logger.exception("Invasion due-state scan failed")
            candidates = []

        for invasion_id, group_id, occurrence_key, status, current_wave in candidates:
            try:
                actions = await cls._process_invasion(int(invasion_id), now_utc)
                for action in actions:
                    if action == "started":
                        stats["invasions_started"] += 1
                    elif action == "restored":
                        stats["invasion_heartbeat_restores"] += 1
                    elif action == "voided":
                        stats["invasions_voided"] += 1
                    elif action in ("wave_started", "wave_finished"):
                        stats["invasion_wave_transitions"] += 1
                    elif action == "wave_finished_and_resolved":
                        stats["invasion_wave_transitions"] += 1
                        stats["invasions_resolved"] += 1
                    elif action == "resolved":
                        stats["invasions_resolved"] += 1
                    elif action == "interrupted":
                        stats["invasions_interrupted"] += 1
                if actions and presentation_ids is not None:
                    # Any committed change (including a restored deadline) must
                    # refresh the pinned screen; stage notices are decided by the
                    # presentation step from persisted state.
                    presentation_ids.append(int(invasion_id))
                if actions:
                    logger.info(
                        "Invasion maintenance actions=%s invasion_id=%s group_id=%s "
                        "occurrence_key=%s",
                        actions,
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

        try:
            settled, failed = await cls._settle_pending_rewards(now_utc)
            stats["invasion_rewards_settled"] += settled
            stats["invasion_failures"] += failed
        except Exception:
            stats["invasion_failures"] += 1
            logger.exception("Invasion reward settlement scan failed")

        if any(stats.values()):
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

        # V2 is the ONLY production invasion cycle. The old
        # run_invasion_cycle implementation remains for Phase 7 cleanup, but
        # running both cycles when the flag is enabled would duplicate work.
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

        # Legacy invasion presentation remains available in
        # _publish_legacy_invasion_updates, but must not run with the v2 tick.
        # Phase 5 will add v2 presentation after the committed DB step.

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
    async def _publish_legacy_invasion_updates(cls, invasion_presentation_ids: list[int]) -> None:
        """Retained v1 presentation path; never called alongside the v2 tick."""
        if await cls._invasion_enabled():
            # [INVASION-REWORK] guarded — see docs/invasion-rework/
            try:
                from sib.services.invasion_telegram import InvasionTelegramService

                await InvasionTelegramService.publish_maintenance_updates(
                    cls._bot,
                    invasion_presentation_ids,
                )
            except Exception:
                logger.exception("Could not publish Invasion group updates")

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
