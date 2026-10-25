"""Come-back reminders: the loop that brings yesterday's player back.

Before this module nothing ever messaged a player unless *they* pressed
something, or an admin typed ``/broadcast`` by hand. The daily chest, the
garden harvest and the invasion all simply waited, so a player who closed
Telegram after their first session was gone for good (see
``docs/growth-diagnosis-fa.md``).

One maintenance tick can now send ONE short reminder per player, and only when
there is something real to say:

``chest``
    Today's chest is still closed and the local hour is past
    ``reminders_chest_after_hour``. The message names the live streak when the
    player has one, which is the whole point of a streak.

``garden``
    At least one plot has fruit ready to harvest.

Hard rules (all enforced here, none of them optional):

* ``reminders_enabled`` is read from ``system_config`` and defaults OFF in
  code — the migration seeds it ON for a fresh install, and an operator can
  turn it off at any time without a deploy.
* A player is messaged at most once per ``reminders_min_gap_hours`` (20h).
  ``user_reminder_states.last_sent_at`` is the per-player clock.
* Messages only go out inside the local evening window
  (``reminders_window_start_hour`` .. ``reminders_window_end_hour``) in
  **Asia/Tehran**, never at 3 a.m.
* ``TelegramForbiddenError`` (the player blocked the bot) writes ``blocked_at``
  and the player is not messaged again unless they switch reminders back on
  from the profile — pressing that switch proves the bot can reach them, so
  ``set_opted_out(False)`` clears the stale block. Any other Telegram failure
  is logged and does not count as "sent", so the player keeps their turn.
* Banned/opted-out/bot accounts are filtered in the query, not in the loop.

The tick is called from ``MaintenanceService.run_once`` behind its own
try/except, so a reminder failure can never affect gameplay work.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.connection import get_session
from sib.database.models import (
    DailyChestClaim,
    GardenPlot,
    User,
    UserReminderState,
)
from sib.services.config_store import ConfigService
from sib.services.daily_chest import utc_day
from sib.services.sib_claim import fa_digits

TEHRAN = ZoneInfo("Asia/Tehran")

KIND_CHEST = "chest"
KIND_GARDEN = "garden"

#: Safe built-in defaults. The migration seeds the same values into
#: ``system_config`` so an operator can see and edit them.
DEFAULT_ENABLED = False
DEFAULT_MIN_GAP_HOURS = 20
DEFAULT_WINDOW_START_HOUR = 16
DEFAULT_WINDOW_END_HOUR = 22
DEFAULT_CHEST_AFTER_HOUR = 17
DEFAULT_BATCH_SIZE = 25


# --------------------------------------------------------------------------- #
# Copy (Persian, short, one clear action)
# --------------------------------------------------------------------------- #
CHEST_TEXT = (
    "🎁 سلام {name}!\n\n"
    "صندوق روزانه‌ات هنوز باز نشده و یه جایزهٔ تصادفی منتظرته "
    "(سیب، نهال، سیب طلایی/الماسی/ابسیدینی و شانسی کارت شخصیت).\n\n"
    "{streak}"
    "👇 بزن و بازش کن."
)

CHEST_STREAK_LINE = "🔥 استریکت {days} روزه است؛ با باز نکردن امروز، از صفر شروع می‌شه!\n\n"

GARDEN_TEXT = (
    "🌳 سلام {name}!\n\n"
    "توی باغت {count} درخت آمادهٔ برداشت است. اگر دیر بکنی روی شاخه می‌مونه "
    "و چرخهٔ بعدی‌اش دیرتر شروع می‌شه.\n\n"
    "👇 برو برداشت کن."
)

CHEST_BUTTON = "🎁 باز کردن صندوق"
GARDEN_BUTTON = "🌳 رفتن به باغ"


@dataclass(frozen=True)
class ReminderTickStats:
    """What one sweep did (logged, and asserted in the tests)."""

    sent: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0
    reason: str = ""

    @property
    def ran(self) -> bool:
        return not self.reason


@dataclass(frozen=True)
class ReminderCandidate:
    user_id: int
    first_name: str
    kind: str
    streak_days: int = 0
    ready_plots: int = 0


class ReminderService:
    """One reminder per player per day, only when there is something to say."""

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @staticmethod
    async def _int_setting(
        session: AsyncSession, key: str, default: int, *, low: int, high: int
    ) -> int:
        raw = await ConfigService.get(session, key)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        if value < low or value > high:
            return default
        return value

    @classmethod
    async def is_enabled(cls, session: AsyncSession) -> bool:
        raw = await ConfigService.get(session, "reminders_enabled")
        if raw is None:
            return DEFAULT_ENABLED
        return bool(raw)

    @classmethod
    async def settings(cls, session: AsyncSession) -> dict:
        return {
            "enabled": await cls.is_enabled(session),
            "min_gap_hours": await cls._int_setting(
                session, "reminders_min_gap_hours", DEFAULT_MIN_GAP_HOURS, low=1, high=72
            ),
            "window_start": await cls._int_setting(
                session,
                "reminders_window_start_hour",
                DEFAULT_WINDOW_START_HOUR,
                low=0,
                high=23,
            ),
            "window_end": await cls._int_setting(
                session,
                "reminders_window_end_hour",
                DEFAULT_WINDOW_END_HOUR,
                low=0,
                high=24,
            ),
            "chest_after": await cls._int_setting(
                session,
                "reminders_chest_after_hour",
                DEFAULT_CHEST_AFTER_HOUR,
                low=0,
                high=23,
            ),
            "batch_size": await cls._int_setting(
                session, "reminders_batch_size", DEFAULT_BATCH_SIZE, low=1, high=200
            ),
        }

    # ------------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------------
    @staticmethod
    def in_window(local_hour: int, start: int, end: int) -> bool:
        """Half-open local window ``[start, end)``; a wrap-around is allowed."""
        if start == end:
            return False
        if start < end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end

    @classmethod
    async def collect(
        cls, session: AsyncSession, *, now: datetime, settings: dict
    ) -> list[ReminderCandidate]:
        """Players who are due a reminder right now, oldest-first.

        Filtering happens in SQL (one query for the players, one for today's
        chest claims, one for ready plots), so the sweep never walks the whole
        user table row by row.
        """
        gap_start = now - timedelta(hours=settings["min_gap_hours"])
        today = utc_day(now)

        due = (
            select(User.id, User.first_name)
            .outerjoin(UserReminderState, UserReminderState.user_id == User.id)
            .where(User.is_bot.is_(False))
            .where(User.is_active.is_(True))
            .where(User.is_banned.is_(False))
            .where(
                or_(
                    UserReminderState.user_id.is_(None),
                    and_(
                        UserReminderState.opted_out.is_(False),
                        UserReminderState.blocked_at.is_(None),
                        or_(
                            UserReminderState.last_sent_at.is_(None),
                            UserReminderState.last_sent_at <= gap_start,
                        ),
                    ),
                )
            )
            # ASC puts never-reminded players (NULL) first on both MariaDB
            # and SQLite; NULLS FIRST is not portable, so it is not used.
            .order_by(UserReminderState.last_sent_at.asc(), User.id.asc())
        )
        users = (await session.execute(due)).all()
        if not users:
            return []

        claimed = set(
            (
                await session.scalars(
                    select(DailyChestClaim.user_id).where(
                        DailyChestClaim.claim_day == today
                    )
                )
            ).all()
        )

        # ``garden_plots.garden_id`` IS the owner's user id (the garden
        # primary key), which keeps the readiness query to a single table.
        ready_rows = (
            await session.execute(
                select(GardenPlot.garden_id, GardenPlot.id).where(
                    GardenPlot.fruit_ready_at.is_not(None),
                    GardenPlot.fruit_ready_at <= now,
                    GardenPlot.is_withered.is_(False),
                )
            )
        ).all()
        ready_counts: dict[int, int] = {}
        for user_id, _plot_id in ready_rows:
            ready_counts[user_id] = ready_counts.get(user_id, 0) + 1

        # "You forgot your chest" is an evening sentence: before
        # ``reminders_chest_after_hour`` the day has only just started.
        chest_allowed = (
            now.astimezone(TEHRAN).hour >= settings["chest_after"]
        )

        candidates: list[ReminderCandidate] = []
        for user_id, first_name in users:
            chest_due = chest_allowed and (user_id not in claimed)
            plots = ready_counts.get(user_id, 0)
            kind = ""
            if chest_due:
                kind = KIND_CHEST
            elif plots:
                kind = KIND_GARDEN
            if not kind:
                continue
            candidates.append(
                ReminderCandidate(
                    user_id=int(user_id),
                    first_name=first_name or "دوست",
                    kind=kind,
                    ready_plots=plots,
                )
            )
            if len(candidates) >= settings["batch_size"]:
                break
        return candidates

    # ------------------------------------------------------------------
    # Copy + delivery
    # ------------------------------------------------------------------
    @staticmethod
    def compose(candidate: ReminderCandidate) -> tuple[str, str]:
        """``(text, button label)`` for one candidate."""
        if candidate.kind == KIND_GARDEN:
            return (
                GARDEN_TEXT.format(
                    name=candidate.first_name,
                    count=fa_digits(candidate.ready_plots),
                ),
                GARDEN_BUTTON,
            )
        streak_line = ""
        if candidate.streak_days >= 2:
            streak_line = CHEST_STREAK_LINE.format(days=fa_digits(candidate.streak_days))
        return (
            CHEST_TEXT.format(name=candidate.first_name, streak=streak_line),
            CHEST_BUTTON,
        )

    @staticmethod
    def _keyboard(candidate: ReminderCandidate):
        from aiogram.types import InlineKeyboardMarkup

        from sib.keyboards.common import success_button

        callback_data = "apple_garden" if candidate.kind == KIND_GARDEN else "apple_chest"
        label = GARDEN_BUTTON if candidate.kind == KIND_GARDEN else CHEST_BUTTON
        return InlineKeyboardMarkup(
            inline_keyboard=[[success_button(label, callback_data)]]
        )

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------
    @classmethod
    async def run_tick(
        cls, bot: Optional[Bot], *, now: Optional[datetime] = None
    ) -> ReminderTickStats:
        """Send the reminders that are due; never raises to the caller."""
        if bot is None:
            return ReminderTickStats(reason="no bot")
        now = now or datetime.now(timezone.utc)

        try:
            async with get_session() as session:
                settings = await cls.settings(session)
                if not settings["enabled"]:
                    return ReminderTickStats(reason="disabled")
                local_hour = now.astimezone(TEHRAN).hour
                if not cls.in_window(
                    local_hour, settings["window_start"], settings["window_end"]
                ):
                    return ReminderTickStats(reason="outside window")

                candidates = await cls.collect(session, now=now, settings=settings)
                if not candidates:
                    return ReminderTickStats(reason="nothing to say")

                # Streak copy needs the per-player streak; only for the chest
                # candidates, and only read-only (this path never pays).
                from sib.services.daily_streak import DailyStreakService

                with_streak: list[ReminderCandidate] = []
                for candidate in candidates:
                    streak_days = 0
                    if candidate.kind == KIND_CHEST:
                        try:
                            summary = await DailyStreakService.summary(
                                session, candidate.user_id
                            )
                            streak_days = summary.days
                        except Exception:  # pragma: no cover - cosmetic only
                            logger.debug(
                                "streak lookup failed for reminder user=%s",
                                candidate.user_id,
                            )
                    with_streak.append(
                        ReminderCandidate(
                            user_id=candidate.user_id,
                            first_name=candidate.first_name,
                            kind=candidate.kind,
                            streak_days=streak_days,
                            ready_plots=candidate.ready_plots,
                        )
                    )
        except Exception:
            logger.exception("reminder sweep could not prepare candidates")
            return ReminderTickStats(reason="prepare failed")

        sent = blocked = failed = 0
        for candidate in with_streak:
            text, _label = cls.compose(candidate)
            try:
                await bot.send_message(
                    candidate.user_id,
                    text,
                    reply_markup=cls._keyboard(candidate),
                    parse_mode=None,
                )
            except TelegramForbiddenError:
                blocked += 1
                await cls._record(candidate, now, outcome="blocked")
                continue
            except Exception:
                failed += 1
                logger.warning(
                    "reminder delivery failed user=%s kind=%s",
                    candidate.user_id,
                    candidate.kind,
                    exc_info=True,
                )
                continue
            sent += 1
            await cls._record(candidate, now, outcome="sent")

        if sent or blocked:
            log_event(
                Events.REMINDERS_SENT,
                {
                    "action": "reminders",
                    "sent": sent,
                    "blocked": blocked,
                    "failed": failed,
                },
            )
        logger.info(
            "reminders: sent=%s blocked=%s failed=%s skipped=%s",
            sent,
            blocked,
            failed,
            len(candidates) - sent - blocked - failed,
        )
        return ReminderTickStats(
            sent=sent,
            skipped=max(0, len(candidates) - sent - blocked - failed),
            blocked=blocked,
            failed=failed,
        )

    @classmethod
    async def _record(
        cls, candidate: ReminderCandidate, now: datetime, *, outcome: str
    ) -> None:
        """Persist what happened to this reminder (its own transaction)."""
        try:
            async with get_session() as session:
                state = await session.get(UserReminderState, candidate.user_id)
                if state is None:
                    state = UserReminderState(user_id=candidate.user_id)
                    session.add(state)
                state.last_kind = candidate.kind
                if outcome == "blocked":
                    state.blocked_at = now
                    state.opted_out = True
                else:
                    state.last_sent_at = now
                await session.flush()
        except Exception:  # pragma: no cover - bookkeeping must never cascade
            logger.exception(
                "could not record reminder state user=%s", candidate.user_id
            )

    # ------------------------------------------------------------------
    # Player-facing toggle
    # ------------------------------------------------------------------
    @classmethod
    async def is_opted_out(cls, session: AsyncSession, user_id: int) -> bool:
        state = await session.get(UserReminderState, user_id)
        return bool(state is not None and (state.opted_out or state.blocked_at is not None))

    @classmethod
    async def set_opted_out(
        cls, session: AsyncSession, user_id: int, opted_out: bool
    ) -> bool:
        """Turn the player's reminders on/off; returns the NEW state."""
        state = await session.get(UserReminderState, user_id)
        if state is None:
            state = UserReminderState(user_id=user_id, opted_out=opted_out)
            session.add(state)
        else:
            state.opted_out = opted_out
            if not opted_out:
                # Pressing the switch proves the player can receive the
                # bot's messages again (a blocked user cannot press it), so a
                # stale ``blocked_at`` is cleared and reminders may resume.
                state.blocked_at = None
        await session.flush()
        return bool(state.opted_out)
