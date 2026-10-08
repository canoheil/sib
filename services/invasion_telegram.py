"""Telegram presentation for persisted Invasion state.

The maintenance path calls this adapter only after the event transaction has
committed. Telegram failures are best-effort presentation failures: the next
maintenance pass retries announcements that still have no persisted message id.
The domain's Invasion rows, not Telegram message history, remain authoritative.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import (
    Faction,
    Invasion,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
)
from sib.keyboards.invasion import get_invasion_keyboard
from sib.services.invasion import InvasionService
from sib.services.telegram_ui import safe_edit_screen, safe_send_screen


MAX_PENDING_INVASIONS_PER_SCAN = 500
MAX_NEW_ANNOUNCEMENTS_PER_CYCLE = 10
ANNOUNCEMENT_RETRY_SECONDS = 60
_ANNOUNCEMENT_RETRY_AFTER: dict[int, float] = {}
_ANNOUNCEMENT_ARTWORK_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "invasion"
    / "moon_descendant_invasion.jpg"
)


def invasion_announcement_photo():
    """Return the default Invasion image, or None for safe text-only fallback."""
    if not _ANNOUNCEMENT_ARTWORK_PATH.is_file():
        return None
    from aiogram.types import FSInputFile

    return FSInputFile(_ANNOUNCEMENT_ARTWORK_PATH)


@dataclass(frozen=True)
class InvasionScreenSnapshot:
    """Read-only screen data copied from the persisted event and aggregates."""

    invasion_id: int
    group_id: int
    status: str
    scheduled_at: datetime
    ended_at: Optional[datetime]
    current_wave_sequence: Optional[int]
    wave_count: int
    current_wave_deadline: Optional[datetime]
    target_region_code: Optional[str]
    target_region_name: Optional[str]
    enemy_faction_code: str
    enemy_faction_name: Optional[str]
    enemy_faction_symbol: Optional[str]
    enemy_power: int
    defense_total: int
    participant_count: int
    outcome: Optional[str]
    announcement_message_id: Optional[int]
    current_wave_defense_total: int = 0
    wave_actionable: bool = False
    viewer_is_participant: Optional[bool] = None
    viewer_contribution_total: Optional[int] = None
    brace_available: bool = False
    brace_used: bool = False


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _remaining_label(deadline: Optional[datetime], now: datetime) -> str:
    if deadline is None:
        return "نامشخص"
    seconds = max(0, math.ceil((_utc(deadline) - _utc(now)).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    pieces = []
    if hours:
        pieces.append(f"{_fa(hours)} ساعت")
    if minutes:
        pieces.append(f"{_fa(minutes)} دقیقه")
    if seconds or not pieces:
        pieces.append(f"{_fa(seconds)} ثانیه")
    return " و ".join(pieces)


def _utc_label(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%d %H:%M UTC").translate(_FA_DIGITS)


def render_invasion_status(
    snapshot: InvasionScreenSnapshot,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Render an announcement/status/result entirely from persisted values."""
    now = _utc(now or datetime.now(timezone.utc))
    faction_name = snapshot.enemy_faction_name or snapshot.enemy_faction_code
    faction = f"{snapshot.enemy_faction_symbol or '🌙'} {faction_name}"
    region = snapshot.target_region_name or snapshot.target_region_code

    if snapshot.status == InvasionStatus.SCHEDULED:
        title = "🌙 تهاجم در راه است"
        wave_label = f"{_fa(1)} از {_fa(snapshot.wave_count)} پس از آغاز"
        timing_label = (
            "⏳ زمان تا آغاز: "
            f"{_remaining_label(snapshot.scheduled_at, now)}"
        )
    elif snapshot.status == InvasionStatus.ACTIVE:
        title = "⚔️ تهاجم آغاز شده است"
        sequence = snapshot.current_wave_sequence
        wave_label = (
            f"موج {_fa(sequence)} از {_fa(snapshot.wave_count)}"
            if sequence is not None
            else f"موج {_fa(snapshot.wave_count)} از {_fa(snapshot.wave_count)}"
        )
        timing_label = (
            "⏳ زمان باقی‌مانده دفاع: "
            f"{_remaining_label(snapshot.current_wave_deadline, now)}"
        )
    else:
        title = "🏁 نتیجه نهایی تهاجم"
        wave_label = f"موج‌ها: {_fa(snapshot.wave_count)} از {_fa(snapshot.wave_count)} تکمیل شد"
        timing_label = "⏳ زمان دفاع: پایان‌یافته"

    lines = [
        title,
        f"⚔️ مهاجم: {faction}",
        f"🗺 منطقه هدف: {region or 'مشخص نشده'}",
        f"💢 قدرت دشمن: {_fa(snapshot.enemy_power)}",
        f"🌊 موج فعلی: {wave_label}",
        f"🛡 دفاع ثبت‌شده: {_fa(snapshot.defense_total)}",
        f"👥 شرکت‌کنندگان: {_fa(snapshot.participant_count)}",
        timing_label,
    ]
    if snapshot.status == InvasionStatus.ACTIVE and snapshot.current_wave_sequence is not None:
        lines.append(
            f"🛡 دفاع این موج: {_fa(snapshot.current_wave_defense_total)}"
        )
    if snapshot.viewer_is_participant:
        lines.append(
            f"👤 سهم شما در یورش: {_fa(snapshot.viewer_contribution_total or 0)}"
        )
        if snapshot.status == InvasionStatus.ACTIVE:
            if snapshot.brace_available:
                action_state = "Brace در دسترس است"
            elif snapshot.brace_used:
                action_state = "Brace را در این موج انجام دادی"
            else:
                action_state = "Brace در این موج در دسترس نیست"
            lines.append(f"🛡 کنش دفاعی این موج: {action_state}")
    if snapshot.status == InvasionStatus.SCHEDULED:
        lines.append(f"🕒 زمان برنامه‌ریزی‌شده: {_utc_label(snapshot.scheduled_at)}")
    elif snapshot.status == InvasionStatus.FINISHED:
        outcome_labels = {
            InvasionOutcome.TOTAL_VICTORY: "پیروزی کامل",
            InvasionOutcome.NARROW_DEFENSE: "دفاع نزدیک",
            InvasionOutcome.DEFEAT: "شکست در دفاع",
        }
        lines.append(
            "🏆 نتیجه: "
            f"{outcome_labels.get(snapshot.outcome, 'ثبت‌نشده')}"
        )
        if snapshot.ended_at is not None:
            lines.append(f"🕒 پایان: {_utc_label(snapshot.ended_at)}")
    return "\n".join(lines)


def invasion_status_keyboard(snapshot: InvasionScreenSnapshot):
    """Build the shared or viewer-specific controls from a loaded snapshot."""
    return get_invasion_keyboard(
        snapshot.invasion_id,
        snapshot.status,
        current_wave_sequence=snapshot.current_wave_sequence,
        wave_actionable=snapshot.wave_actionable,
        viewer_is_participant=snapshot.viewer_is_participant,
    )


async def _load_snapshot(
    invasion_id: int,
    *,
    viewer_user_id: Optional[int] = None,
) -> Optional[InvasionScreenSnapshot]:
    async with get_session() as session:
        invasion = await session.scalar(
            select(Invasion)
            .where(Invasion.id == invasion_id)
            .options(selectinload(Invasion.waves))
        )
        if invasion is None:
            return None

        faction = await session.scalar(
            select(Faction).where(Faction.code == invasion.enemy_faction_code)
        )
        current_wave = next(
            (
                wave
                for wave in invasion.waves
                if wave.wave_sequence == invasion.current_wave_sequence
            ),
            None,
        )
        defense_total = await session.scalar(
            select(func.coalesce(func.sum(InvasionWave.contribution_total), 0)).where(
                InvasionWave.invasion_id == invasion.id
            )
        )
        participant_count = await session.scalar(
            select(func.count(InvasionParticipant.id)).where(
                InvasionParticipant.invasion_id == invasion.id
            )
        )
        now = datetime.now(timezone.utc)
        action_state = await InvasionService.get_defense_action_availability(
            session,
            invasion.id,
            viewer_user_id,
            now=now,
        )
        wave_actionable = action_state.wave_actionable
        viewer_state = action_state if viewer_user_id is not None else None
        return InvasionScreenSnapshot(
            invasion_id=int(invasion.id),
            group_id=int(invasion.group_id),
            status=invasion.status,
            scheduled_at=invasion.scheduled_at,
            ended_at=invasion.ended_at,
            current_wave_sequence=invasion.current_wave_sequence,
            wave_count=len(invasion.waves),
            current_wave_deadline=(
                current_wave.deadline_at if current_wave is not None else None
            ),
            target_region_code=invasion.target_region_code,
            target_region_name=invasion.target_region_name_snapshot,
            enemy_faction_code=invasion.enemy_faction_code,
            enemy_faction_name=faction.name_fa if faction is not None else None,
            enemy_faction_symbol=faction.symbol if faction is not None else None,
            enemy_power=int(invasion.enemy_power),
            defense_total=int(defense_total or 0),
            participant_count=int(participant_count or 0),
            outcome=invasion.outcome,
            announcement_message_id=invasion.announcement_message_id,
            current_wave_defense_total=(
                int(current_wave.contribution_total)
                if current_wave is not None
                else 0
            ),
            wave_actionable=wave_actionable,
            viewer_is_participant=(
                viewer_state.participant_id is not None
                if viewer_state is not None
                else None
            ),
            viewer_contribution_total=(
                viewer_state.participant_total if viewer_state is not None else None
            ),
            brace_available=(
                viewer_state.brace_available if viewer_state is not None else False
            ),
            brace_used=(viewer_state.brace_used if viewer_state is not None else False),
        )


async def _persist_announcement_id(
    snapshot: InvasionScreenSnapshot,
    message_id: int,
    *,
    expected_message_id: Optional[int],
) -> bool:
    try:
        async with get_session() as session:
            saved = await InvasionService.set_announcement_message_id(
                session,
                snapshot.invasion_id,
                message_id,
                expected_group_id=snapshot.group_id,
                expected_message_id=expected_message_id,
            )
        if not saved:
            logger.warning(
                "Invasion announcement id compare-and-set lost invasion_id=%s "
                "group_id=%s expected_message_id=%s new_message_id=%s",
                snapshot.invasion_id,
                snapshot.group_id,
                expected_message_id,
                message_id,
            )
        return saved
    except Exception:
        logger.exception(
            "Could not persist Invasion announcement id invasion_id=%s group_id=%s "
            "message_id=%s",
            snapshot.invasion_id,
            snapshot.group_id,
            message_id,
        )
        return False


async def _try_delete_message(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as error:  # pragma: no cover - best-effort cleanup
        logger.warning(
            "Could not delete replaced Invasion announcement chat_id=%s "
            "message_id=%s: %s",
            chat_id,
            message_id,
            error,
        )


async def _send_announcement(bot, snapshot: InvasionScreenSnapshot) -> bool:
    text = render_invasion_status(snapshot)
    keyboard = invasion_status_keyboard(snapshot)
    sent = await safe_send_screen(
        bot,
        snapshot.group_id,
        text,
        photo=invasion_announcement_photo(),
        reply_markup=keyboard,
        parse_mode=None,
    )
    message_id = getattr(sent, "message_id", None) if sent is not None else None
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        logger.warning(
            "Invasion announcement send failed invasion_id=%s group_id=%s",
            snapshot.invasion_id,
            snapshot.group_id,
        )
        return False
    saved = await _persist_announcement_id(
        snapshot,
        message_id,
        expected_message_id=None,
    )
    if saved:
        _ANNOUNCEMENT_RETRY_AFTER.pop(snapshot.invasion_id, None)
    return saved


async def _edit_announcement(bot, snapshot: InvasionScreenSnapshot) -> bool:
    current_message_id = snapshot.announcement_message_id
    if current_message_id is None:
        return await _send_announcement(bot, snapshot)

    text = render_invasion_status(snapshot)
    keyboard = invasion_status_keyboard(snapshot)
    replacement_id = await safe_edit_screen(
        bot,
        snapshot.group_id,
        current_message_id,
        text,
        photo=invasion_announcement_photo(),
        reply_markup=keyboard,
        parse_mode=None,
        current_is_photo=None,
    )
    if replacement_id is not None:
        if replacement_id != current_message_id:
            return await _persist_announcement_id(
                snapshot,
                replacement_id,
                expected_message_id=current_message_id,
            )
        return True

    # The stored message may have been deleted or become uneditable. Recover
    # with a fresh current-state announcement after the failed edit; then move
    # the persisted pointer only if it still names the message we tried.
    sent = await safe_send_screen(
        bot,
        snapshot.group_id,
        text,
        photo=invasion_announcement_photo(),
        reply_markup=keyboard,
        parse_mode=None,
    )
    new_message_id = getattr(sent, "message_id", None) if sent is not None else None
    if (
        isinstance(new_message_id, bool)
        or not isinstance(new_message_id, int)
        or new_message_id <= 0
    ):
        logger.warning(
            "Invasion announcement update failed invasion_id=%s group_id=%s "
            "message_id=%s",
            snapshot.invasion_id,
            snapshot.group_id,
            current_message_id,
        )
        return False
    saved = await _persist_announcement_id(
        snapshot,
        new_message_id,
        expected_message_id=current_message_id,
    )
    if saved:
        await _try_delete_message(bot, snapshot.group_id, current_message_id)
    return saved


class InvasionTelegramService:
    """Post-commit presentation hook used by the existing maintenance sweep."""

    @classmethod
    async def publish_maintenance_updates(
        cls,
        bot,
        changed_invasion_ids: Optional[list[int]] = None,
    ) -> int:
        """Announce new rows and edit messages after persisted transitions.

        New/unannounced messages are capped per maintenance pass and retried
        using only the existing `announcement_message_id IS NULL` condition.
        Edits are attempted only for event ids whose persisted lifecycle moved;
        this avoids a five-second edit loop. The in-memory retry clock is only a
        Telegram rate limiter, never a source of Invasion state.
        """
        if bot is None:
            return 0

        changed_ids = list(dict.fromkeys(int(item) for item in (changed_invasion_ids or [])))
        async with get_session() as session:
            pending_ids = list(
                (
                    await session.scalars(
                        select(Invasion.id)
                        .where(Invasion.announcement_message_id.is_(None))
                        .order_by(Invasion.created_at.asc(), Invasion.id.asc())
                        .limit(MAX_PENDING_INVASIONS_PER_SCAN)
                    )
                ).all()
            )

        candidates = list(dict.fromkeys(changed_ids + [int(item) for item in pending_ids]))
        new_announcements = 0
        published = 0
        now_monotonic = time.monotonic()
        # Expired retry entries no longer protect a live pending message and
        # should not accumulate if an event is manually deleted.
        for retry_id, retry_after in tuple(_ANNOUNCEMENT_RETRY_AFTER.items()):
            if now_monotonic >= retry_after:
                _ANNOUNCEMENT_RETRY_AFTER.pop(retry_id, None)
        for invasion_id in candidates:
            snapshot = None
            try:
                snapshot = await _load_snapshot(invasion_id)
                if snapshot is None:
                    continue
                if snapshot.announcement_message_id is None:
                    if new_announcements >= MAX_NEW_ANNOUNCEMENTS_PER_CYCLE:
                        continue
                    retry_after = _ANNOUNCEMENT_RETRY_AFTER.get(invasion_id, 0.0)
                    if now_monotonic < retry_after:
                        continue
                    new_announcements += 1
                    _ANNOUNCEMENT_RETRY_AFTER[invasion_id] = (
                        now_monotonic + ANNOUNCEMENT_RETRY_SECONDS
                    )
                    if await _send_announcement(bot, snapshot):
                        published += 1
                    continue

                if invasion_id not in changed_ids:
                    continue
                if await _edit_announcement(bot, snapshot):
                    published += 1
            except Exception:
                logger.exception(
                    "Invasion Telegram presentation failed invasion_id=%s "
                    "group_id=%s",
                    invasion_id,
                    snapshot.group_id if snapshot is not None else None,
                )
        return published

    @staticmethod
    async def load_snapshot(
        invasion_id: int,
        *,
        viewer_user_id: Optional[int] = None,
    ) -> Optional[InvasionScreenSnapshot]:
        """Load persisted screen state, optionally including one viewer's state."""
        return await _load_snapshot(invasion_id, viewer_user_id=viewer_user_id)

    @staticmethod
    async def persist_replacement_id(
        snapshot: InvasionScreenSnapshot,
        new_message_id: int,
        *,
        expected_message_id: Optional[int],
    ) -> bool:
        """Persist a `safe_edit_screen` result in a separate transaction."""
        return await _persist_announcement_id(
            snapshot,
            new_message_id,
            expected_message_id=expected_message_id,
        )

    @staticmethod
    async def send_group_notice(bot, group_id: int, text: str) -> None:
        """Best-effort plain-text callback feedback in its originating Group."""
        await safe_send_screen(bot, group_id, text, parse_mode=None)


__all__ = [
    "InvasionScreenSnapshot",
    "InvasionTelegramService",
    "invasion_announcement_photo",
    "invasion_status_keyboard",
    "render_invasion_status",
]
