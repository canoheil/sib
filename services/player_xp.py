"""Authoritative Account XP awards, daily limits, and Level 1–30 progress.

Every XP-producing domain action calls this service inside the same database
transaction as its source mutation. The progression row is locked before
caps are calculated, and the grant ledger's unique key is the retry-safe
source of truth. Level is always derived from cumulative XP here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import PlayerProgression, PlayerXPGrant, User

MAX_LEVEL = 30
MAX_TOTAL_XP = 87_000
GLOBAL_DAILY_XP_CAP = 1_000
NOTIFICATION_CLAIM_TIMEOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class XPSourcePolicy:
    """Fixed economy policy for one approved source (daily caps are UTC)."""

    amount: int
    daily_event_cap: Optional[int] = None
    daily_xp_cap: Optional[int] = None
    win_bonus: int = 0


# The single allow-list and balance registry for account XP sources. New
# sources are not paid until explicitly entered here and reviewed against the
# common daily cap. Daily event caps count eligible source events; the Flower
# cap is an XP cap because both sender and recipient can earn from one gift.
XP_SOURCE_POLICIES: dict[str, XPSourcePolicy] = {
    "normal_match": XPSourcePolicy(amount=100, daily_event_cap=8, win_bonus=25),
    "bot_match": XPSourcePolicy(amount=20, daily_event_cap=2),
    "friendly_match": XPSourcePolicy(amount=35, daily_event_cap=5),
    "garden_harvest": XPSourcePolicy(amount=5, daily_event_cap=5),
    "garden_plant": XPSourcePolicy(amount=10),
    "garden_unlock": XPSourcePolicy(amount=15),
    "garden_magic": XPSourcePolicy(amount=10),
    "card_collection": XPSourcePolicy(amount=25),
    "card_upgrade": XPSourcePolicy(amount=10),
    "trade": XPSourcePolicy(amount=10, daily_event_cap=1),
    "flower": XPSourcePolicy(amount=5, daily_xp_cap=10),
    "first_marriage": XPSourcePolicy(amount=50),
}

# A compact text/emoji visual, keyed by every exact level. It is presentation
# only; no generated artwork or gameplay bonus is attached to these badges.
_LEVEL_BADGES = {
    1: "🍃",
    2: "🌱",
    3: "🌿",
    4: "🍀",
    5: "🍏",
    6: "🍎",
    7: "🌳",
    8: "🌲",
    9: "🌼",
    10: "🌸",
    11: "🥉",
    12: "🥈",
    13: "🥇",
    14: "🏅",
    15: "🏆",
    16: "⭐",
    17: "🌟",
    18: "✨",
    19: "💫",
    20: "🔆",
    21: "🔥",
    22: "⚡",
    23: "🌊",
    24: "💎",
    25: "🛡️",
    26: "⚔️",
    27: "🦅",
    28: "🐉",
    29: "🌌",
    30: "👑",
}


@dataclass(frozen=True)
class LevelProgress:
    """Derived Account Level and current-level XP display values."""

    total_xp: int
    level: int
    current_level_xp: int
    xp_to_next_level: Optional[int]

    @property
    def is_max(self) -> bool:
        return self.level == MAX_LEVEL

    @property
    def remaining_xp(self) -> int:
        if self.xp_to_next_level is None:
            return 0
        return max(0, self.xp_to_next_level - self.current_level_xp)


@dataclass(frozen=True)
class XPAwardResult:
    """Outcome of one source event, including its actual capped grant."""

    user_id: int
    source: str
    event_key: str
    requested_xp: int
    granted_xp: int
    total_xp_before: int
    total_xp_after: int
    level_before: int
    level_after: int
    duplicate: bool = False
    grant_id: Optional[int] = None

    @property
    def leveled_up(self) -> bool:
        return self.level_after > self.level_before


@dataclass(frozen=True)
class _LevelUpNotice:
    user_id: int
    from_level: int
    to_level: int
    grant_ids: tuple[int, ...]
    chat_id: Optional[int]


class PlayerXPService:
    """The one write/read path for personal Account XP and Level progress."""

    @staticmethod
    def cumulative_xp_for_level(level: int) -> int:
        """Lifetime XP required to reach ``level`` (Level 1 starts at zero)."""
        level = int(level)
        if not 1 <= level <= MAX_LEVEL:
            raise ValueError(f"level must be between 1 and {MAX_LEVEL}")
        steps = level - 1
        # Sum (50 + 10*L^2) for L = 1..level-1; integer arithmetic only.
        return 50 * steps + 10 * steps * level * (2 * level - 1) // 6

    @staticmethod
    def xp_required_for_next_level(level: int) -> Optional[int]:
        """Incremental threshold from ``level`` to ``level + 1``."""
        level = int(level)
        if not 1 <= level < MAX_LEVEL:
            return None
        return 50 + 10 * level * level

    @classmethod
    def get_level_progress(cls, total_xp: int) -> LevelProgress:
        """Derive level/current bar from cumulative XP, clamped at Level 30."""
        bounded_xp = min(MAX_TOTAL_XP, max(0, int(total_xp)))
        if bounded_xp >= MAX_TOTAL_XP:
            return LevelProgress(
                total_xp=MAX_TOTAL_XP,
                level=MAX_LEVEL,
                current_level_xp=0,
                xp_to_next_level=None,
            )

        # Thirty levels is deliberately small; keeping this straightforward
        # makes the threshold definition auditable and has no practical cost.
        for level in range(1, MAX_LEVEL):
            next_threshold = cls.cumulative_xp_for_level(level + 1)
            if bounded_xp < next_threshold:
                current_threshold = cls.cumulative_xp_for_level(level)
                return LevelProgress(
                    total_xp=bounded_xp,
                    level=level,
                    current_level_xp=bounded_xp - current_threshold,
                    xp_to_next_level=next_threshold - current_threshold,
                )

        # The MAX branch above makes this unreachable, but retain a defensive
        # return if the cap/curve are changed later.
        return LevelProgress(MAX_TOTAL_XP, MAX_LEVEL, 0, None)

    @classmethod
    def level_badge(cls, level: int) -> str:
        """Presentation-only visual selected by the exact derived level."""
        bounded = min(MAX_LEVEL, max(1, int(level)))
        return _LEVEL_BADGES[bounded]

    @classmethod
    def format_profile_progress(cls, progress: LevelProgress) -> str:
        """Persian Level block shared by all existing personal-profile paths."""
        badge = cls.level_badge(progress.level)
        if progress.is_max:
            return (
                f"🎖 **سطح حساب: {badge} سطح {progress.level} — MAX**\n"
                f"⚡ XP کل: `{MAX_TOTAL_XP:,} / {MAX_TOTAL_XP:,}`"
            )

        required = int(progress.xp_to_next_level or 0)
        filled = (
            min(10, max(0, round(progress.current_level_xp * 10 / required)))
            if required
            else 0
        )
        bar = "🟩" * filled + "⬜" * (10 - filled)
        return (
            f"🎖 **سطح حساب: {badge} سطح {progress.level}**\n"
            f"{bar} `{progress.current_level_xp:,} / {required:,} XP`"
            f" · کل: `{progress.total_xp:,}`"
        )

    @classmethod
    async def get_progress(
        cls, session: AsyncSession, user_id: int
    ) -> LevelProgress:
        """Read progression without locking; absent rows render as Level 1."""
        row = await session.get(PlayerProgression, int(user_id))
        return cls.get_level_progress(row.total_xp if row is not None else 0)

    @classmethod
    async def _lock_progression(
        cls, session: AsyncSession, user_id: int
    ) -> Optional[PlayerProgression]:
        """Lock the user's progression counter, creating a missing test/legacy row.

        Rollout migrations pre-create every existing row and UserService
        creates rows for new accounts. The savepoint is a defensive recovery
        for imported accounts and direct model fixtures; it avoids rolling
        back the source operation if another transaction won the insert race.
        """
        row = await session.scalar(
            select(PlayerProgression)
            .where(PlayerProgression.user_id == user_id)
            .with_for_update()
        )
        if row is not None:
            return row

        if await session.get(User, user_id) is None:
            return None

        try:
            async with session.begin_nested():
                session.add(PlayerProgression(user_id=user_id, total_xp=0))
                await session.flush()
        except IntegrityError:
            # Concurrent account/progression creation; the unique account PK
            # is the arbiter. Re-read the winner below.
            pass

        return await session.scalar(
            select(PlayerProgression)
            .where(PlayerProgression.user_id == user_id)
            .with_for_update()
        )

    @staticmethod
    def set_notification_chat(session: AsyncSession, chat_id: Optional[int]) -> None:
        """Set the source chat fallback for XP notices in this transaction."""
        if chat_id is not None:
            session.info["player_xp_notification_chat_id"] = int(chat_id)

    @classmethod
    async def award(
        cls,
        session: AsyncSession,
        *,
        user_id: int,
        source: str,
        event_key: str,
        won: bool = False,
        notification_chat_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> XPAwardResult:
        """Apply one approved XP event exactly once, transactionally.

        Daily source caps are evaluated before the global 1,000-XP cap. The
        per-user progression row serializes concurrent awards so two events
        cannot both spend the same daily allowance.
        """
        source = str(source)
        policy = XP_SOURCE_POLICIES.get(source)
        if policy is None:
            # Unknown sources default to no XP and fail loudly during
            # development rather than silently introducing an unbudgeted path.
            raise ValueError(f"Unregistered XP source: {source}")
        if won and policy.win_bonus <= 0:
            raise ValueError(f"XP source {source!r} has no win bonus")

        user_id = int(user_id)
        event_key = str(event_key)
        if not event_key or len(event_key) > 255:
            raise ValueError("XP event_key must contain 1..255 characters")

        user = await session.get(User, user_id)
        if user is None or user.is_bot:
            return XPAwardResult(
                user_id=user_id,
                source=source,
                event_key=event_key,
                requested_xp=policy.amount + (policy.win_bonus if won else 0),
                granted_xp=0,
                total_xp_before=0,
                total_xp_after=0,
                level_before=1,
                level_after=1,
            )

        progression = await cls._lock_progression(session, user_id)
        if progression is None:
            return XPAwardResult(
                user_id=user_id,
                source=source,
                event_key=event_key,
                requested_xp=policy.amount + (policy.win_bonus if won else 0),
                granted_xp=0,
                total_xp_before=0,
                total_xp_after=0,
                level_before=1,
                level_after=1,
            )

        before_total = int(progression.total_xp)
        before_progress = cls.get_level_progress(before_total)
        requested = policy.amount + (policy.win_bonus if won else 0)

        duplicate = await session.scalar(
            select(PlayerXPGrant).where(
                PlayerXPGrant.user_id == user_id,
                PlayerXPGrant.source == source,
                PlayerXPGrant.event_key == event_key,
            )
        )
        if duplicate is not None:
            after_progress = cls.get_level_progress(before_total)
            return XPAwardResult(
                user_id=user_id,
                source=source,
                event_key=event_key,
                requested_xp=requested,
                granted_xp=0,
                total_xp_before=before_total,
                total_xp_after=before_total,
                level_before=before_progress.level,
                level_after=after_progress.level,
                duplicate=True,
                grant_id=duplicate.id,
            )

        now_utc = cls._as_utc(now or datetime.now(timezone.utc))
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        daily_scope = (
            PlayerXPGrant.user_id == user_id,
            PlayerXPGrant.awarded_at >= day_start,
            PlayerXPGrant.awarded_at < day_end,
        )

        # First enforce the source's event-count limit or XP-specific limit.
        source_remaining = requested
        if policy.daily_event_cap is not None:
            source_events = await session.scalar(
                select(func.count(PlayerXPGrant.id)).where(
                    *daily_scope,
                    PlayerXPGrant.source == source,
                )
            )
            if int(source_events or 0) >= policy.daily_event_cap:
                source_remaining = 0
        if policy.daily_xp_cap is not None:
            source_total = await session.scalar(
                select(func.coalesce(func.sum(PlayerXPGrant.amount), 0)).where(
                    *daily_scope,
                    PlayerXPGrant.source == source,
                )
            )
            source_remaining = min(
                source_remaining,
                max(0, policy.daily_xp_cap - int(source_total or 0)),
            )

        daily_total = await session.scalar(
            select(func.coalesce(func.sum(PlayerXPGrant.amount), 0)).where(
                *daily_scope
            )
        )
        daily_remaining = max(
            0, GLOBAL_DAILY_XP_CAP - int(daily_total or 0)
        )
        lifetime_remaining = max(0, MAX_TOTAL_XP - before_total)
        granted = min(
            requested,
            source_remaining,
            daily_remaining,
            lifetime_remaining,
        )

        after_total = before_total + granted
        after_progress = cls.get_level_progress(after_total)
        effective_chat_id = notification_chat_id
        if effective_chat_id is None:
            effective_chat_id = session.info.get("player_xp_notification_chat_id")
        if effective_chat_id is not None:
            effective_chat_id = int(effective_chat_id)

        grant = PlayerXPGrant(
            user_id=user_id,
            source=source,
            event_key=event_key,
            amount=granted,
            awarded_at=now_utc,
            level_before=before_progress.level,
            level_after=after_progress.level,
            notification_chat_id=effective_chat_id,
        )
        progression.total_xp = after_total
        session.add(grant)
        await session.flush()

        log_event(
            Events.PLAYER_XP_AWARDED,
            {
                "user_id": user_id,
                "source": source,
                "event_key": event_key,
                "requested_xp": requested,
                "granted_xp": granted,
                "total_xp": after_total,
                "level_before": before_progress.level,
                "level_after": after_progress.level,
            },
        )
        if after_progress.level > before_progress.level:
            log_event(
                Events.PLAYER_LEVEL_UP,
                {
                    "user_id": user_id,
                    "from_level": before_progress.level,
                    "to_level": after_progress.level,
                    "source": source,
                    "event_key": event_key,
                },
            )

        return XPAwardResult(
            user_id=user_id,
            source=source,
            event_key=event_key,
            requested_xp=requested,
            granted_xp=granted,
            total_xp_before=before_total,
            total_xp_after=after_total,
            level_before=before_progress.level,
            level_after=after_progress.level,
            duplicate=False,
            grant_id=grant.id,
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    async def award_character_collection(
        cls,
        session: AsyncSession,
        *,
        user_id: int,
        character_id: int,
        notification_chat_id: Optional[int] = None,
    ) -> XPAwardResult:
        """One-time 25 XP collection milestone (shop or completed trade)."""
        character_id = int(character_id)
        return await cls.award(
            session,
            user_id=user_id,
            source="card_collection",
            event_key=f"card_collection:{character_id}",
            notification_chat_id=notification_chat_id,
        )

    @classmethod
    async def award_match_results(
        cls,
        session: AsyncSession,
        match,
        *,
        started: bool,
    ) -> list[XPAwardResult]:
        """Classify a completed match and pay each real human at most once.

        Apple Battle intentionally comes through this same path exactly once;
        its separate Apple/seedling settlement remains unchanged.
        """
        if not started or match.id is None:
            return []

        from sib.database.models import MatchPlayer, MatchType

        if "players" in match.__dict__:
            players = list(match.players)
        else:
            players = list(
                (
                    await session.scalars(
                        select(MatchPlayer).where(MatchPlayer.match_id == match.id)
                    )
                ).all()
            )
        if not players:
            return []

        users = {
            user.id: user
            for user in (
                await session.scalars(
                    select(User).where(User.id.in_([p.user_id for p in players]))
                )
            ).all()
        }
        human_players = [
            p
            for p in players
            if (user := users.get(int(p.user_id))) is not None and not user.is_bot
        ]
        if not human_players:
            return []

        distinct_humans = {int(p.user_id) for p in human_players}
        friendly = match.match_type == MatchType.FRIENDLY
        if friendly and len(distinct_humans) >= 2:
            source = "friendly_match"
        elif match.is_bot_match or len(distinct_humans) < 2:
            source = "bot_match"
        else:
            source = "normal_match"

        winner_team = getattr(match, "winner_team", None)
        results: list[XPAwardResult] = []
        # One deterministic user order avoids progression-row deadlocks when
        # simultaneous matches share participants in reverse team order.
        for player in sorted(human_players, key=lambda p: int(p.user_id)):
            won = (
                source == "normal_match"
                and not bool(getattr(match, "is_draw", False))
                and winner_team is not None
                and int(player.team) == int(winner_team)
            )
            chat_id = getattr(player, "group_id", None)
            if chat_id is None:
                chat_id = getattr(match, "group1_id", None) or getattr(
                    match, "group2_id", None
                )
            results.append(
                await cls.award(
                    session,
                    user_id=int(player.user_id),
                    source=source,
                    event_key=f"match:{int(match.id)}",
                    won=won,
                    notification_chat_id=chat_id,
                )
            )
        return results

    @classmethod
    async def deliver_pending_notifications(
        cls,
        bot,
        *,
        fallback_chat_id: Optional[int] = None,
        grant_ids: Optional[list[int]] = None,
        limit: int = 100,
    ) -> int:
        """Send committed level-up notices and mark successful deliveries.

        The grant ledger itself is the progression-specific outbox. A short
        claim lease prevents overlapping handlers/maintenance cycles from
        sending the same message concurrently; undelivered rows are retried
        by the existing MaintenanceService. Telegram delivery always happens
        after the XP/source transaction has committed.
        """
        if bot is None:
            return 0

        from sib.database.connection import get_session

        now = datetime.now(timezone.utc)
        stale_claim = now - NOTIFICATION_CLAIM_TIMEOUT
        try:
            async with get_session() as session:
                stmt = (
                    select(PlayerXPGrant)
                    .where(
                        PlayerXPGrant.level_after > PlayerXPGrant.level_before,
                        PlayerXPGrant.notification_sent_at.is_(None),
                        or_(
                            PlayerXPGrant.notification_claimed_at.is_(None),
                            PlayerXPGrant.notification_claimed_at < stale_claim,
                        ),
                    )
                    .order_by(PlayerXPGrant.id.asc())
                    .limit(max(1, int(limit)))
                    .with_for_update()
                )
                if grant_ids is not None:
                    ids = [int(grant_id) for grant_id in grant_ids]
                    if not ids:
                        return 0
                    stmt = stmt.where(PlayerXPGrant.id.in_(ids))
                rows = list((await session.scalars(stmt)).all())
                if not rows:
                    return 0

                for row in rows:
                    row.notification_claimed_at = now
                await session.flush()

                # Snapshot values before the transaction releases its row locks.
                notices = cls._coalesce_notices(rows, fallback_chat_id)
        except Exception:  # pragma: no cover - post-commit notification I/O
            logger.exception("Could not claim pending XP level-up notifications")
            return 0

        delivered_count = 0
        for notice in notices:
            direct_text = (
                f"🎉 تبریک! سطح حسابت از {notice.from_level} به "
                f"سطح {notice.to_level} رسید. 🍏"
            )
            delivered = False
            try:
                await bot.send_message(chat_id=notice.user_id, text=direct_text)
                delivered = True
            except Exception as direct_error:  # pragma: no cover - Telegram I/O
                chat_id = notice.chat_id
                if chat_id is not None and int(chat_id) != notice.user_id:
                    group_text = (
                        f'🎉 به <a href="tg://user?id={notice.user_id}">بازیکن</a> '
                        f'تبریک! از سطح {notice.from_level} به سطح {notice.to_level} رسید. 🍏'
                    )
                    try:
                        await bot.send_message(
                            chat_id=int(chat_id),
                            text=group_text,
                            parse_mode="HTML",
                        )
                        delivered = True
                    except Exception as group_error:  # pragma: no cover
                        logger.warning(
                            "Could not deliver XP level-up notice for user %s "
                            "(private: %s, fallback: %s)",
                            notice.user_id,
                            direct_error,
                            group_error,
                        )
                else:
                    logger.warning(
                        "Could not deliver private XP level-up notice for user %s: %s",
                        notice.user_id,
                        direct_error,
                    )

            try:
                async with get_session() as session:
                    values = {
                        "notification_claimed_at": None,
                    }
                    if delivered:
                        values["notification_sent_at"] = datetime.now(timezone.utc)
                        delivered_count += 1
                    await session.execute(
                        update(PlayerXPGrant)
                        .where(
                            PlayerXPGrant.id.in_(notice.grant_ids),
                            PlayerXPGrant.notification_sent_at.is_(None),
                        )
                        .values(**values)
                    )
            except Exception:  # pragma: no cover - post-commit persistence
                logger.exception(
                    "Could not finalize XP level-up notification for user %s",
                    notice.user_id,
                )

        return delivered_count

    @staticmethod
    def _coalesce_notices(
        rows: list[PlayerXPGrant], fallback_chat_id: Optional[int]
    ) -> list[_LevelUpNotice]:
        """Combine multiple transitions for a user into one X→Y message."""
        grouped: dict[int, list[PlayerXPGrant]] = {}
        for row in rows:
            grouped.setdefault(int(row.user_id), []).append(row)

        notices = []
        for user_id, user_rows in grouped.items():
            user_rows.sort(key=lambda row: int(row.id))
            chat_id = next(
                (
                    int(row.notification_chat_id)
                    for row in reversed(user_rows)
                    if row.notification_chat_id is not None
                ),
                int(fallback_chat_id) if fallback_chat_id is not None else None,
            )
            notices.append(
                _LevelUpNotice(
                    user_id=user_id,
                    from_level=int(user_rows[0].level_before),
                    to_level=int(user_rows[-1].level_after),
                    grant_ids=tuple(int(row.id) for row in user_rows),
                    chat_id=chat_id,
                )
            )
        return notices
