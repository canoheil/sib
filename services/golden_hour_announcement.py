"""Per-group Golden Hour announcements, coalesced with invasion messages."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import (
    GoldenHourGroupAnnouncement,
    Group,
    Inv2Invasion,
)
from sib.services.config_store import (
    ConfigService,
    DEFAULT_INVASION_SLOT_WINDOW_MINUTES,
    DEFAULT_INVASION_SPAWN_HOURS,
)
from sib.services.golden_hour import TEHRAN
from sib.services.invasion_v2.enums import InvasionStatus


# Kept as one exact string in the database-backed invasion narrative so later
# caption edits/finalization can preserve the same notice without duplicating it.
GOLDEN_HOUR_NOTICE_TEXT = (
    "✨ ساعت طلایی از ۲۰:۰۰ تا پیش از ۲۱:۰۰ تهران فعاله؛ پاداش‌های "
    "واجدشرایط بازی دوبرابر می‌شن. بوس و بغل دوستانه هم شامل می‌شه."
)

_NOTICE_RETRYABLE_SOURCES = frozenset({"invasion", "invasion_edit"})
_STALE_EDIT_CLAIM = timedelta(minutes=5)
_STANDALONE_GRACE = timedelta(minutes=5)


def _as_utc(moment: datetime | None) -> datetime:
    """Use UTC for naive persisted timestamps; never use server local time."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def tehran_day(moment: datetime | None = None) -> date:
    """The durable idempotency date is the local Asia/Tehran calendar day."""
    return _as_utc(moment).astimezone(TEHRAN).date()


async def claim_golden_hour_notice(
    session: AsyncSession,
    group_id: int,
    *,
    source: str,
    now: datetime,
    invasion_id: int | None = None,
) -> bool:
    """Claim the group's one daily notice, serializing workers by its PK.

    Failed photo/edit deliveries may be retried by the same invasion flow: an
    identical caption edit is idempotent, and a pending invasion already has
    its own recovery behavior. A failed standalone send is deliberately not
    retried automatically because Telegram may have accepted it before a
    connection error was reported.
    """
    day = tehran_day(now)
    attempted_at = _as_utc(now)
    existing = await session.scalar(
        select(GoldenHourGroupAnnouncement)
        .where(
            GoldenHourGroupAnnouncement.group_id == group_id,
            GoldenHourGroupAnnouncement.announcement_day == day,
        )
        .with_for_update()
    )
    if existing is not None:
        retry_failed = (
            existing.status == "failed"
            and existing.source == source
            and source in _NOTICE_RETRYABLE_SOURCES
        )
        retry_stale_edit = (
            existing.status == "sending"
            and existing.source == source == "invasion_edit"
            and _as_utc(existing.attempted_at)
            <= attempted_at - _STALE_EDIT_CLAIM
        )
        if retry_failed or retry_stale_edit:
            existing.status = "sending"
            existing.attempt_count += 1
            existing.attempted_at = attempted_at
            existing.error = None
            existing.invasion_id = invasion_id or existing.invasion_id
            return True
        return False

    record = GoldenHourGroupAnnouncement(
        group_id=group_id,
        announcement_day=day,
        status="sending",
        source=source,
        invasion_id=invasion_id,
        attempt_count=1,
        attempted_at=attempted_at,
    )
    try:
        # The savepoint turns a concurrent insert into a harmless lost claim
        # instead of leaving the caller's surrounding transaction unusable.
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        logger.debug(
            "Golden Hour notice claim already exists group=%s day=%s", group_id, day
        )
        return False
    return True


async def mark_golden_hour_notice_sent(
    session: AsyncSession,
    group_id: int,
    *,
    day: date,
    message_id: int | None,
    now: datetime,
) -> None:
    record = await session.get(GoldenHourGroupAnnouncement, (group_id, day))
    if record is None:
        return
    record.status = "sent"
    record.sent_at = _as_utc(now)
    record.message_id = message_id
    record.error = None


async def mark_golden_hour_notice_failed(
    session: AsyncSession,
    group_id: int,
    *,
    day: date,
    error: str,
) -> None:
    record = await session.get(GoldenHourGroupAnnouncement, (group_id, day))
    if record is None:
        return
    record.status = "failed"
    record.error = str(error)[:2000]


def _spawn_window_minutes(raw: object) -> int:
    try:
        value = int(raw)
        if value < 0:
            raise ValueError("must be non-negative")
        return value
    except (TypeError, ValueError):
        logger.warning(
            "Invalid invasion_slot_window_minutes for Golden Hour notice; using %s",
            DEFAULT_INVASION_SLOT_WINDOW_MINUTES,
        )
        return int(DEFAULT_INVASION_SLOT_WINDOW_MINUTES)


def _spawn_hours(raw: object) -> set[int]:
    try:
        hours = {int(part.strip()) for part in str(raw).split(",") if part.strip()}
        if any(hour < 0 or hour > 23 for hour in hours):
            raise ValueError("hours must be between 0 and 23")
        return hours
    except (TypeError, ValueError):
        logger.warning(
            "Invalid invasion_spawn_hours for Golden Hour notice; treating 20:00 as no spawn slot"
        )
        return set()


def _standalone_due(local_now: datetime, *, spawn_hours: set[int], window_minutes: int) -> bool:
    """Wait for today's 20:00 spawn slot to close before a standalone post.

    The scheduler cannot spawn after the 20:00 local hour even if its
    configured minute window is 60 or longer, so the effective window is
    capped at 60 minutes. A five-minute grace catches a slightly delayed
    maintenance tick; with the default 30-minute window, the notice is due
    from 20:30 through the end of Golden Hour.
    """
    local_start = local_now.replace(hour=20, minute=0, second=0, microsecond=0)
    effective_window = min(window_minutes, 60) if 20 in spawn_hours else 0
    due_at = local_start + timedelta(minutes=effective_window)
    valid_until = max(due_at + _STANDALONE_GRACE, local_start + timedelta(hours=1))
    return due_at <= local_now <= valid_until


class GoldenHourGroupAnnouncementService:
    """Send one notice per active group, preferring an existing invasion post."""

    @classmethod
    async def run_due(cls, bot, *, now: datetime | None = None) -> int:
        """Publish due group notices and return the number delivered.

        The 20:00 invasion slot (default 30-minute spawn window) gets first
        chance to announce. When a group has a live invasion, its existing
        photo caption is edited instead of sending another group message.
        Otherwise a short standalone message is sent after that spawn window.
        """
        if bot is None:
            return 0
        moment = _as_utc(now)
        local_now = moment.astimezone(TEHRAN)
        if local_now.hour not in (20, 21) or (
            local_now.hour == 21 and local_now.minute > 5
        ):
            return 0

        try:
            async with get_session() as session:
                raw_hours = await ConfigService.get(session, "invasion_spawn_hours")
                raw_window = await ConfigService.get(
                    session, "invasion_slot_window_minutes"
                )
                groups = list(
                    (
                        await session.scalars(
                            select(Group)
                            .where(Group.is_active.is_(True))
                            .order_by(Group.id)
                        )
                    ).all()
                )
            hours = _spawn_hours(
                DEFAULT_INVASION_SPAWN_HOURS if raw_hours is None else raw_hours
            )
            window_minutes = _spawn_window_minutes(
                DEFAULT_INVASION_SLOT_WINDOW_MINUTES
                if raw_window is None
                else raw_window
            )
        except Exception:
            logger.exception("Could not prepare Golden Hour group announcements")
            return 0

        if not _standalone_due(
            local_now, spawn_hours=hours, window_minutes=window_minutes
        ):
            return 0

        delivered = 0
        day = local_now.date()
        for group in groups:
            try:
                if await cls._append_to_live_invasion(
                    bot, group.id, day=day, now=moment
                ):
                    delivered += 1
                    continue
                if await cls._send_standalone(
                    bot, group.id, day=day, now=moment
                ):
                    delivered += 1
            except Exception:
                # One group's failed database or Telegram operation must not
                # starve the remaining active groups.
                logger.exception(
                    "Golden Hour group notice failed group=%s day=%s",
                    group.id,
                    day,
                )
        return delivered

    @classmethod
    async def _append_to_live_invasion(
        cls, bot, group_id: int, *, day: date, now: datetime
    ) -> bool:
        """Edit a live invasion's existing photo caption; never send a second post.

        Returns True only when the caption edit delivered the notice. False
        means a standalone attempt is safe: either there was no live invasion,
        or a durable claim already prevents a duplicate. Database locks are
        released before Telegram I/O so a notice edit cannot hold up joins.
        """
        try:
            from sib.services.invasion_v2.presentation import _apply_hp_edit

            invasion_id = None
            message_id = None
            async with get_session() as session:
                invasions = list(
                    (
                        await session.scalars(
                            select(Inv2Invasion)
                            .where(
                                Inv2Invasion.group_id == group_id,
                                Inv2Invasion.status == InvasionStatus.ACTIVE.value,
                                Inv2Invasion.message_id.is_not(None),
                            )
                            .order_by(
                                Inv2Invasion.started_at.desc(), Inv2Invasion.id.desc()
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                invasion = next(
                    (
                        item
                        for item in invasions
                        if item.ends_at is not None and _as_utc(item.ends_at) > now
                    ),
                    None,
                )
                if invasion is None:
                    return False

                claimed = await claim_golden_hour_notice(
                    session,
                    group_id,
                    source="invasion_edit",
                    now=now,
                    invasion_id=invasion.id,
                )
                if not claimed:
                    # A sent/sending/failed row is already the durable decision
                    # for today. The standalone claim will also be rejected.
                    return False

                narrative_text = invasion.announcement_narrative
                if not narrative_text:
                    await mark_golden_hour_notice_failed(
                        session,
                        group_id,
                        day=day,
                        error="active invasion has no stored announcement narrative",
                    )
                    return False
                if GOLDEN_HOUR_NOTICE_TEXT not in narrative_text:
                    invasion.announcement_narrative = (
                        f"{narrative_text}\n\n{GOLDEN_HOUR_NOTICE_TEXT}"
                    )
                invasion_id = invasion.id
                message_id = invasion.message_id

            # The narrative is durable before editing. _apply_hp_edit reads the
            # latest committed HP, and a retry of the same caption is harmless.
            success = await _apply_hp_edit(bot, invasion_id)
            async with get_session() as session:
                if success:
                    await mark_golden_hour_notice_sent(
                        session,
                        group_id,
                        day=day,
                        message_id=message_id,
                        now=now,
                    )
                else:
                    await mark_golden_hour_notice_failed(
                        session,
                        group_id,
                        day=day,
                        error="Telegram rejected the invasion caption edit",
                    )
            return success
        except Exception:
            logger.exception(
                "Could not merge Golden Hour notice into invasion group=%s", group_id
            )
            # The durable claim (if any) blocks a duplicate standalone attempt.
            return False

    @classmethod
    async def _send_standalone(
        cls, bot, group_id: int, *, day: date, now: datetime
    ) -> bool:
        """Claim durably before sending, so retries/restarts cannot duplicate."""
        async with get_session() as session:
            claimed = await claim_golden_hour_notice(
                session,
                group_id,
                source="standalone",
                now=now,
            )
        if not claimed:
            return False

        try:
            message = await bot.send_message(
                chat_id=group_id,
                text=GOLDEN_HOUR_NOTICE_TEXT,
                disable_web_page_preview=True,
            )
            if message is None:
                error = "Telegram returned no message"
                message_id = None
                delivered = False
            else:
                error = ""
                message_id = getattr(message, "message_id", None)
                delivered = True
        except Exception as exc:
            logger.warning(
                "Golden Hour standalone message failed group=%s", group_id,
                exc_info=True,
            )
            error = str(exc) or exc.__class__.__name__
            message_id = None
            delivered = False

        async with get_session() as session:
            if delivered:
                await mark_golden_hour_notice_sent(
                    session,
                    group_id,
                    day=day,
                    message_id=message_id,
                    now=datetime.now(timezone.utc),
                )
            else:
                await mark_golden_hour_notice_failed(
                    session,
                    group_id,
                    day=day,
                    error=error,
                )
        return delivered
