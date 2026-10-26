"""Daily chest streak: the reason to come back tomorrow.

The chest already pays once per UTC day (``daily_chest_claims``), so the
streak needs no new table and no new column: it is *derived* from the claim
ledger. A streak is the number of consecutive UTC days, ending today (or
yesterday when today is still unclaimed), on which the player opened the
chest.

Milestones pay extra Apples through the auditable wallet ledger, exactly once
per day: the credit carries the idempotency key ``daily_streak:<user>:<day>``,
so a retried or duplicated claim can never pay the bonus twice, and the bonus
lives in the same transaction as the claim row (a failed opening rolls back
both).

Everything is configurable from ``system_config.daily_streak_rewards``
(``{"3": 30, "7": 100, "14": 250}`` by default) - rebalancing is a config
edit, not a deploy.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import DailyChestClaim
from sib.services.apple_wallet import AppleWalletService
from sib.services.daily_chest import utc_day
from sib.services.golden_hour import reward_multiplier as golden_hour_multiplier

#: Configuration key (JSON object: milestone day -> extra Apples).
STREAK_REWARDS_KEY = "daily_streak_rewards"

#: Built-in milestones. Day 3 is reachable in the first week, day 7 is the
#: habit, day 14 is the "I actually play this" badge; values are deliberately
#: small next to a 300-Apple card so a streak accelerates progress without
#: replacing the economy.
DEFAULT_STREAK_REWARDS: dict[int, int] = {3: 30, 7: 100, 14: 250}

#: How far back the ledger is read. Anything older cannot be part of a live
#: streak a player would notice, and the window keeps the query bounded.
STREAK_WINDOW_DAYS = 60

STREAK_REASON = "daily_streak"
STREAK_KEY_FORMAT = "daily_streak:{user_id}:{day}"


@dataclass(frozen=True)
class StreakSummary:
    """One player's streak as of ``day``.

    ``days`` counts consecutive days including today when ``day`` itself is
    claimed. ``bonus_apples`` is what the milestone paid for THIS day (0 when
    the day is not a milestone). ``next_milestone``/``days_to_next`` drive the
    "come back tomorrow" copy; both are None once every milestone is behind.
    """

    days: int
    bonus_apples: int = 0
    next_milestone: Optional[int] = None
    days_to_next: Optional[int] = None

    @property
    def has_next(self) -> bool:
        return self.next_milestone is not None and self.days_to_next is not None


class DailyStreakService:
    """Derive streaks from the chest ledger and pay milestone bonuses."""

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @staticmethod
    def parse_rewards(raw) -> dict[int, int]:
        """Validate ``daily_streak_rewards``; fall back to the defaults.

        Only positive milestone days with a non-negative Apple amount are
        accepted. A broken override must never take the streak (or the chest)
        down, so the caller gets the built-in table instead.
        """
        if not isinstance(raw, dict):
            return dict(DEFAULT_STREAK_REWARDS)
        parsed: dict[int, int] = {}
        for key, value in raw.items():
            if isinstance(key, bool):
                return dict(DEFAULT_STREAK_REWARDS)
            try:
                day = int(key)
            except (TypeError, ValueError):
                return dict(DEFAULT_STREAK_REWARDS)
            if day <= 0 or isinstance(value, bool):
                return dict(DEFAULT_STREAK_REWARDS)
            try:
                apples = int(value)
            except (TypeError, ValueError):
                return dict(DEFAULT_STREAK_REWARDS)
            if apples < 0:
                return dict(DEFAULT_STREAK_REWARDS)
            parsed[day] = apples
        return parsed or dict(DEFAULT_STREAK_REWARDS)

    @classmethod
    async def rewards(cls, session: AsyncSession) -> dict[int, int]:
        from sib.services.config_store import ConfigService

        return cls.parse_rewards(await ConfigService.get(session, STREAK_REWARDS_KEY))

    # ------------------------------------------------------------------
    # Streak arithmetic
    # ------------------------------------------------------------------
    @staticmethod
    async def _claimed_days(
        session: AsyncSession, user_id: int, *, since: str
    ) -> set[str]:
        rows = await session.scalars(
            select(DailyChestClaim.claim_day).where(
                DailyChestClaim.user_id == user_id,
                DailyChestClaim.claim_day >= since,
            )
        )
        return set(rows.all())

    @classmethod
    async def count_streak(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        day: str,
    ) -> int:
        """Consecutive claimed days ending at ``day`` (or yesterday).

        ``day`` itself does not have to be claimed: before the player opens
        today's chest, the live streak is the one that ended yesterday, and
        that is what the screen advertises ("استریک ۳ روزه — امروز رو از دست
        نده"). Once today is claimed, the same call counts it.
        """
        since = (date.fromisoformat(day) - timedelta(days=STREAK_WINDOW_DAYS)).isoformat()
        days = await cls._claimed_days(session, user_id, since=since)

        cursor = date.fromisoformat(day)
        if cursor.isoformat() not in days:
            cursor -= timedelta(days=1)
        streak = 0
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    @classmethod
    async def summary(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        day: Optional[str] = None,
        pay_bonus: bool = False,
        now: Optional[datetime] = None,
    ) -> StreakSummary:
        """Streak + milestone state. Pays the bonus only when asked.

        ``pay_bonus`` is used by the chest claim itself: it runs inside the
        claim transaction, after the claim row exists, so the streak that the
        bonus is paid on is exactly the streak the player is about to see.
        Every other caller (screen rendering, reminders) reads only.
        """
        moment = now or datetime.now(timezone.utc)
        day = day or utc_day(moment)
        rewards = await cls.rewards(session)
        streak = await cls.count_streak(session, user_id, day=day)

        bonus = 0
        if pay_bonus and streak > 0 and streak in rewards:
            apples = rewards[streak]
            if apples > 0:
                idempotency_key = STREAK_KEY_FORMAT.format(
                    user_id=user_id, day=day
                )
                multiplier = golden_hour_multiplier(STREAK_REASON, now=moment)
                await AppleWalletService.earn(
                    session,
                    user_id,
                    apples,
                    reason=STREAK_REASON,
                    idempotency_key=idempotency_key,
                    reward_multiplier=multiplier,
                    occurred_at=moment,
                )
                transaction = await AppleWalletService.get_transaction_by_idempotency_key(
                    session, idempotency_key, for_update=True
                )
                bonus = int(transaction.amount) if transaction is not None else apples * multiplier
            log_event(
                Events.DAILY_STREAK,
                {
                    "action": STREAK_REASON,
                    "user_id": user_id,
                    "day": day,
                    "streak": streak,
                    "apples": bonus,
                },
            )
        elif pay_bonus and streak > 0:
            logger.debug("daily streak %s for user %s (no milestone)", streak, user_id)

        upcoming = sorted(milestone for milestone in rewards if milestone > streak)
        next_milestone = upcoming[0] if upcoming else None
        return StreakSummary(
            days=streak,
            bonus_apples=bonus,
            next_milestone=next_milestone,
            days_to_next=(
                next_milestone - streak if next_milestone is not None else None
            ),
        )
