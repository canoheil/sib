"""Daily Disease & Doctor group announcement.

The maintenance worker calls :func:`run_disease_announcement` on every cycle.
At the configured Asia/Tehran time (18:15 by default, shortly after the
18:00 infection tick) it posts one short message per active group naming the
Doctor of the Day and the sick players in that group.

``last_disease_announcement_date`` in ``system_config`` is the durable
idempotency key: the worker claims the Tehran calendar date in its own
transaction BEFORE any Telegram I/O, so a crash, a restart, or a second
worker can never repost the same day. A failed delivery is therefore not
retried until the next Tehran day - the same trade-off the disease reminders
make to avoid duplicating a message players already saw.
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from sqlalchemy import select
from sqlalchemy.dialects import mysql, sqlite

from sib.core.logging import logger
from sib.core.text import format_html, plain_display_name
from sib.database.connection import get_session
from sib.database.models import (
    Group,
    Inv2GroupMember,
    SystemConfig,
    User,
    UserDisease,
)
from sib.services.config_store import ConfigService
from sib.services.disease_service import TEHRAN, _as_utc
from sib.services.sib_claim import fa_digits

ANNOUNCEMENT_STATE_KEY = "last_disease_announcement_date"
ANNOUNCEMENT_HINT = "⚠️ برای درمان، دستور /disease را بزنید یا منتظر دکتر بمانید!"
MAX_MENTIONED_PATIENTS = 3
DUE_WINDOW_MINUTES = 15
DEFAULT_HOUR = 18
DEFAULT_MINUTE = 15


def _as_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        folded = value.strip().lower()
        if folded in {"true", "1", "yes", "on"}:
            return True
        if folded in {"false", "0", "no", "off", ""}:
            return False
    return default


def _clamp_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


class DiseaseAnnouncementSettings(NamedTuple):
    enabled: bool
    hour: int
    minute: int


async def load_settings(session) -> DiseaseAnnouncementSettings:
    enabled = _as_bool(
        await ConfigService.get(session, "disease_announcement_enabled"), True
    )
    hour = _clamp_int(
        await ConfigService.get(session, "disease_announcement_hour"),
        default=DEFAULT_HOUR, minimum=0, maximum=23,
    )
    minute = _clamp_int(
        await ConfigService.get(session, "disease_announcement_minute"),
        default=DEFAULT_MINUTE, minimum=0, maximum=59,
    )
    return DiseaseAnnouncementSettings(enabled, hour, minute)


def announcement_due(
    local_now: datetime, *, hour: int, minute: int, window_minutes: int = DUE_WINDOW_MINUTES
) -> bool:
    """True inside the configured Tehran window (18:15-18:30 by default)."""
    start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= local_now < start + timedelta(minutes=window_minutes)


def user_mention(user_id: int, first_name: Optional[str]) -> str:
    name = plain_display_name(first_name, "بازیکن")
    return f"[{name}](tg://user?id={int(user_id)})"


def format_disease_announcement(
    doctor: Optional[tuple[int, Optional[str]]],
    sick_users: list[tuple[int, Optional[str]]],
    *,
    max_listed: int = MAX_MENTIONED_PATIENTS,
) -> str:
    """Render the group message; up to ``max_listed`` mentions, then a count."""
    if doctor is not None:
        doctor_line = f"🩺 **پزشک امروز:** {user_mention(doctor[0], doctor[1])}"
    else:
        doctor_line = "🩺 **پزشک امروز:** هنوز انتخاب نشده است."
    if sick_users:
        shown = list(sick_users[:max_listed])
        mentions = "، ".join(user_mention(user_id, name) for user_id, name in shown)
        extra = len(sick_users) - len(shown)
        if extra > 0:
            mentions += f" و {fa_digits(extra)} نفر دیگر"
        sick_line = f"🦠 **بیماران امروز:** {mentions}"
    else:
        sick_line = "🦠 **بیماران امروز:** هیچ بیماری در این گروه نیست."
    return "\n".join((doctor_line, sick_line, ANNOUNCEMENT_HINT))


async def _claim_announcement_day(session, today: str) -> bool:
    """Claim ``last_disease_announcement_date`` for one Tehran day.

    Upsert the mutex row, then lock it - the first committed claim wins and
    every concurrent worker sees the stored date and backs off.
    """
    dialect = session.get_bind().dialect.name
    values = {"key": ANNOUNCEMENT_STATE_KEY, "value": "null"}
    if dialect in ("mysql", "mariadb"):
        stmt = mysql.insert(SystemConfig).values(**values)
        stmt = stmt.on_duplicate_key_update(key=SystemConfig.key)
    elif dialect == "sqlite":
        stmt = sqlite.insert(SystemConfig).values(**values).on_conflict_do_nothing(
            index_elements=["key"]
        )
    else:
        raise RuntimeError(f"Unsupported disease announcement dialect: {dialect}")
    await session.execute(stmt)
    row = await session.scalar(
        select(SystemConfig).where(SystemConfig.key == ANNOUNCEMENT_STATE_KEY)
        .with_for_update().execution_options(populate_existing=True)
    )
    if row is not None and row.value == today:
        return False
    row.value = today
    await session.flush()
    return True


async def _sick_group_members(session, group_id: int) -> list[tuple[int, Optional[str]]]:
    rows = (
        await session.execute(
            select(User.id, User.first_name)
            .join(Inv2GroupMember, Inv2GroupMember.user_id == User.id)
            .join(UserDisease, UserDisease.user_id == User.id)
            .where(
                Inv2GroupMember.group_id == group_id,
                UserDisease.infection_level > 0,
                User.is_active.is_(True),
                User.is_bot.is_(False),
                User.is_banned.is_(False),
            )
            .order_by(User.id)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


async def _announcement_payloads(session, moment: datetime):
    from sib.handlers.disease import current_doctor

    groups = list(
        (
            await session.scalars(
                select(Group).where(Group.is_active.is_(True)).order_by(Group.id)
            )
        ).all()
    )
    doctor_id = await current_doctor(session, now=moment)
    doctor = None
    if doctor_id is not None:
        doctor_name = await session.scalar(
            select(User.first_name).where(User.id == doctor_id)
        )
        doctor = (doctor_id, doctor_name)
    payloads = []
    for group in groups:
        sick = await _sick_group_members(session, group.id)
        payloads.append((group.id, format_disease_announcement(doctor, sick)))
    return payloads


async def _send_group_announcement(bot, group_id: int, text: str) -> bool:
    try:
        await bot.send_message(
            chat_id=group_id,
            text=format_html(text),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception:
        # One unreachable group must not starve the others (or roll anything
        # back); the claim already protects against a same-day repost.
        logger.exception("Disease daily announcement failed group=%s", group_id)
        return False


async def run_disease_announcement(bot, *, now: Optional[datetime] = None) -> int:
    """Post the daily announcement to every active group once per Tehran day."""
    if bot is None:
        return 0
    moment = _as_utc(now or datetime.now(timezone.utc))
    async with get_session() as session:
        settings = await load_settings(session)
    if not settings.enabled:
        return 0
    local_now = moment.astimezone(TEHRAN)
    if not announcement_due(local_now, hour=settings.hour, minute=settings.minute):
        return 0
    today = local_now.date().isoformat()
    async with get_session() as session:
        if not await _claim_announcement_day(session, today):
            return 0
    async with get_session() as session:
        payloads = await _announcement_payloads(session, moment)
    sent = 0
    for group_id, text in payloads:
        if await _send_group_announcement(bot, group_id, text):
            sent += 1
    if sent:
        logger.info("Disease daily announcement sent to %s group(s)", sent)
    return sent
