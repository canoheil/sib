"""Telegram presentation for persisted Invasion state.

The maintenance path calls this adapter only after the event transaction has
committed. Telegram failures are best-effort presentation failures: the next
maintenance pass retries announcements that still have no persisted message id.
The domain's Invasion rows, not Telegram message history, remain authoritative.

A freshly persisted announcement message is also pinned in its Group (quietly,
so pinning never spams a service message). Pinning is presentation-only: the
previous Sib Invasion announcement of the Group - identified solely through
ids Sib itself persisted - is unpinned first so pinned announcements do not
accumulate, and every pin/unpin failure is logged without touching the
Invasion, its transaction, or the persisted announcement id.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Optional

from sqlalchemy import and_, func, or_, select
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
# Retry clock per (invasion id, target stage) for wave/result notices.
_STAGE_NOTICE_RETRY_AFTER: dict[tuple[int, int], float] = {}
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
    # Four-wave battles only (None for legacy events): the delivered stage, the
    # battle's actual activation time, and the settled Golden Apple amount per
    # defender (None when no reward was paid).
    presentation_stage: Optional[int] = None
    started_at: Optional[datetime] = None
    reward_golden_apples: Optional[int] = None
    rewards_settled: bool = False


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
        if snapshot.outcome is None and snapshot.started_at is not None:
            wave_label = "موج‌ها: پس از قطع طولانی ربات، بدون نتیجه بسته شد"
        elif snapshot.outcome is None:
            wave_label = "موج‌ها: بدون آغاز لغو شد"
        else:
            wave_label = (
                f"موج‌ها: {_fa(snapshot.wave_count)} از "
                f"{_fa(snapshot.wave_count)} تکمیل شد"
            )
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
    if (
        snapshot.presentation_stage is not None
        and snapshot.started_at is not None
        and snapshot.status == InvasionStatus.ACTIVE
    ):
        lines.append(f"🕒 آغاز یورش: {_utc_label(snapshot.started_at)}")
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
        if snapshot.presentation_stage is not None:
            lines.append(_reward_line(snapshot))
    return "\n".join(lines)


_OUTCOME_LABELS = {
    InvasionOutcome.TOTAL_VICTORY: "پیروزی کامل",
    InvasionOutcome.NARROW_DEFENSE: "دفاع نزدیک",
    InvasionOutcome.DEFEAT: "شکست در دفاع",
}


def _reward_line(snapshot: InvasionScreenSnapshot) -> str:
    """Golden Apple reward status for a finished four-wave battle.

    Interrupted battles (no outcome) never carry a reward. Amounts are granted
    to defenders only, as inventory items (🌕 سیب طلایی), not wallet Apples.
    """
    if snapshot.outcome is None:
        return "🎁 پاداشی تعلق نمی‌گیرد."
    if snapshot.reward_golden_apples is not None:
        return (
            f"🎁 پاداش: {_fa(snapshot.reward_golden_apples)} 🌕 سیب طلایی "
            "برای هر مدافع واجد شرایط"
        )
    if snapshot.rewards_settled:
        return "🎁 پاداشی ثبت نشد؛ مدافع واجد شرایطی وجود نداشت."
    return "🎁 پاداش در حال ثبت است؛ به‌زودی به حساب شما اضافه می‌شود."


def presentation_target(snapshot: InvasionScreenSnapshot) -> Optional[int]:
    """Stage a four-wave snapshot should have delivered: wave N, or N + 1 result.

    Returns None for legacy events (``presentation_stage`` is NULL) and for any
    state with nothing to present.
    """
    if snapshot.presentation_stage is None:
        return None
    if snapshot.status == InvasionStatus.ACTIVE and snapshot.current_wave_sequence is not None:
        return int(snapshot.current_wave_sequence)
    if snapshot.status == InvasionStatus.FINISHED:
        return int(snapshot.wave_count) + 1
    return None


def _faction_line(snapshot: InvasionScreenSnapshot) -> str:
    faction_name = snapshot.enemy_faction_name or snapshot.enemy_faction_code
    return f"⚔️ مهاجم: {snapshot.enemy_faction_symbol or '🌙'} {faction_name}"


def _region_line(snapshot: InvasionScreenSnapshot) -> str:
    region = snapshot.target_region_name or snapshot.target_region_code
    return f"🗺 منطقه هدف: {region or 'مشخص نشده'}"


def render_wave_notice(snapshot: InvasionScreenSnapshot, *, now: Optional[datetime] = None) -> str:
    """Short, persisted-state notice posted once when a wave begins."""
    now = _utc(now or datetime.now(timezone.utc))
    sequence = snapshot.current_wave_sequence or 1
    return "\n".join(
        [
            f"🌊 موج {_fa(sequence)} از {_fa(snapshot.wave_count)} آغاز شد",
            _faction_line(snapshot),
            _region_line(snapshot),
            f"⏳ زمان دفاع این موج: {_remaining_label(snapshot.current_wave_deadline, now)}",
            f"🛡 دفاع این موج: {_fa(snapshot.current_wave_defense_total)}",
            f"👥 شرکت‌کنندگان: {_fa(snapshot.participant_count)}",
            "هر شرکت‌کننده در هر موج یک بار می‌تواند دفاع کند؛ دکمه 🛡 Brace را بزن.",
        ]
    )


def render_final_notice(snapshot: InvasionScreenSnapshot) -> str:
    """One-time result notice for a finished four-wave battle."""
    lines = ["🏁 یورش به پایان رسید", _faction_line(snapshot), _region_line(snapshot)]
    if snapshot.outcome is None:
        if snapshot.started_at is not None:
            # Activated, then closed because bot downtime exceeded the recovery
            # budget. Players were not defeated, so no result is recorded.
            lines.append(
                "⛔️ این یورش به‌دلیل قطع طولانی ربات بدون نتیجه بسته شد؛ "
                "نه پیروزی ثبت شد نه شکست، و پاداشی تعلق نمی‌گیرد."
            )
        else:
            lines.append("🚫 این یورش بدون دفاع لغو شد؛ پاداشی تعلق نمی‌گیرد.")
        return "\n".join(lines)
    lines.extend(
        [
            f"🏆 نتیجه: {_OUTCOME_LABELS.get(snapshot.outcome, 'ثبت‌نشده')}",
            f"🛡 دفاع کل: {_fa(snapshot.defense_total)} از {_fa(snapshot.enemy_power)} قدرت دشمن",
            f"👥 شرکت‌کنندگان: {_fa(snapshot.participant_count)}",
            _reward_line(snapshot),
        ]
    )
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


async def _settled_reward_golden_apples(session, invasion) -> Optional[int]:
    """Golden Apples each defender received, if this battle's reward was paid.

    Read from persisted state: the settled marker, the approved outcome, and
    the existence of at least one defense action. The amount is the configured
    value for that outcome, which settlement applies in the same transaction.
    """
    from sib.database.models import InvasionDefenseAction, InvasionOutcome
    from sib.services.invasion_rewards import InvasionRewardService

    if invasion.rewards_settled_at is None or invasion.outcome not in InvasionOutcome.ALL:
        return None
    defenders = await session.scalar(
        select(func.count(func.distinct(InvasionDefenseAction.participant_id))).where(
            InvasionDefenseAction.invasion_id == invasion.id
        )
    )
    if not defenders:
        return None
    return await InvasionRewardService.configured_amount(session, invasion.outcome)


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
            presentation_stage=invasion.presentation_stage,
            started_at=invasion.started_at,
            reward_golden_apples=await _settled_reward_golden_apples(session, invasion),
            rewards_settled=invasion.rewards_settled_at is not None,
        )


async def _persist_announcement_id(
    snapshot: InvasionScreenSnapshot,
    message_id: int,
    *,
    expected_message_id: Optional[int],
    presentation_stage: Optional[int] = None,
) -> bool:
    try:
        async with get_session() as session:
            saved = await InvasionService.set_announcement_message_id(
                session,
                snapshot.invasion_id,
                message_id,
                expected_group_id=snapshot.group_id,
                expected_message_id=expected_message_id,
                presentation_stage=presentation_stage,
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


async def _previous_announcement_message_id(
    group_id: int,
    *,
    exclude_invasion_id: int,
    exclude_message_id: int,
) -> Optional[int]:
    """Latest announcement id Sib itself persisted for another Invasion here.

    Only ids stored on this Group's own Invasion rows are considered, so the
    result can never name an unrelated admin/user message. The newest other
    occurrence is the one the previous pin most likely still points at.
    """
    async with get_session() as session:
        value = await session.scalar(
            select(Invasion.announcement_message_id)
            .where(
                Invasion.group_id == group_id,
                Invasion.id != exclude_invasion_id,
                Invasion.announcement_message_id.is_not(None),
                Invasion.announcement_message_id != exclude_message_id,
            )
            .order_by(Invasion.scheduled_at.desc(), Invasion.id.desc())
            .limit(1)
        )
    return int(value) if value is not None else None


async def _pin_announcement(
    bot,
    snapshot: InvasionScreenSnapshot,
    message_id: int,
) -> bool:
    """Pin a freshly persisted announcement; presentation-only, never raises.

    The previous Sib Invasion announcement of this Group is unpinned first so
    pinned announcements do not accumulate. That unpin is targeted at an id
    Sib itself persisted for this Group: the current pinned message is never
    unpinned blindly and an unrelated admin/user message is never touched.
    Pin/unpin failures (API errors, missing Bot permissions) are logged and
    swallowed: the Invasion stays valid, the committed announcement id stays
    usable, and no transaction is rolled back. Successful pinning is not a
    domain requirement.
    """
    try:
        previous_id = await _previous_announcement_message_id(
            snapshot.group_id,
            exclude_invasion_id=snapshot.invasion_id,
            exclude_message_id=message_id,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Could not load the previous Invasion announcement "
            "group_id=%s invasion_id=%s",
            snapshot.group_id,
            snapshot.invasion_id,
        )
        previous_id = None

    if previous_id is not None:
        try:
            await bot.unpin_chat_message(
                chat_id=snapshot.group_id,
                message_id=previous_id,
            )
        except Exception as error:
            logger.warning(
                "Could not unpin the previous Invasion announcement "
                "chat_id=%s message_id=%s invasion_id=%s: %s",
                snapshot.group_id,
                previous_id,
                snapshot.invasion_id,
                error,
            )

    try:
        # Quiet pin: a notification service message every cadence slot would
        # spam the Group; the pinned announcement itself is the notice.
        await bot.pin_chat_message(
            chat_id=snapshot.group_id,
            message_id=message_id,
            disable_notification=True,
        )
        return True
    except Exception as error:
        logger.warning(
            "Could not pin the Invasion announcement chat_id=%s message_id=%s "
            "invasion_id=%s: %s",
            snapshot.group_id,
            message_id,
            snapshot.invasion_id,
            error,
        )
        return False


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
        # Presentation-only follow-up: a pin failure never invalidates the
        # committed announcement or the Invasion itself.
        await _pin_announcement(bot, snapshot, message_id)
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
            saved = await _persist_announcement_id(
                snapshot,
                replacement_id,
                expected_message_id=current_message_id,
            )
            if saved:
                # safe_edit_screen replaced (and deleted) the pinned message;
                # the replacement is the announcement from now on.
                await _pin_announcement(bot, snapshot, replacement_id)
            return saved
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
        # The replacement message is the announcement from now on; the
        # deleted message loses its pin, so re-pin the replacement.
        await _pin_announcement(bot, snapshot, new_message_id)
    return saved


async def _persist_presentation_stage(
    snapshot: InvasionScreenSnapshot,
    *,
    expected_stage: Optional[int],
    new_stage: int,
) -> bool:
    try:
        async with get_session() as session:
            saved = await InvasionService.set_presentation_stage(
                session,
                snapshot.invasion_id,
                expected_stage=expected_stage,
                new_stage=new_stage,
            )
        if not saved:
            logger.warning(
                "Invasion presentation stage compare-and-set lost invasion_id=%s "
                "expected=%s new=%s",
                snapshot.invasion_id,
                expected_stage,
                new_stage,
            )
        return saved
    except Exception:
        logger.exception(
            "Could not persist Invasion presentation stage invasion_id=%s",
            snapshot.invasion_id,
        )
        return False


async def _publish_four_wave(
    bot,
    snapshot: InvasionScreenSnapshot,
    *,
    changed: bool,
    may_send,
) -> bool:
    """Deliver the next persisted stage of one four-wave battle.

    Order inside one pass: announcement (once) -> wave/result notice ->
    stage compare-and-set -> in-place refresh of the pinned screen. The stage
    is recorded only after its notice was sent, so a failed send is retried
    and a delivered one is never repeated.
    """
    target = presentation_target(snapshot)
    if target is None:
        return False

    if snapshot.announcement_message_id is None:
        if not may_send(snapshot.invasion_id):
            return False
        sent = await safe_send_screen(
            bot,
            snapshot.group_id,
            render_invasion_status(snapshot),
            photo=invasion_announcement_photo(),
            reply_markup=invasion_status_keyboard(snapshot),
            parse_mode=None,
        )
        message_id = getattr(sent, "message_id", None) if sent is not None else None
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            logger.warning(
                "Invasion battle announcement send failed invasion_id=%s group_id=%s",
                snapshot.invasion_id,
                snapshot.group_id,
            )
            return False
        saved = await _persist_announcement_id(
            snapshot,
            message_id,
            expected_message_id=None,
            presentation_stage=target,
        )
        if saved:
            _ANNOUNCEMENT_RETRY_AFTER.pop(snapshot.invasion_id, None)
            await _pin_announcement(bot, snapshot, message_id)
        return saved

    delivered = snapshot.presentation_stage or 0
    if delivered < target:
        retry_key = (snapshot.invasion_id, target)
        if time.monotonic() < _STAGE_NOTICE_RETRY_AFTER.get(retry_key, 0.0):
            return False
        if not may_send(snapshot.invasion_id, announcement=False):
            return False
        final = target > int(snapshot.wave_count)
        if final:
            text = render_final_notice(snapshot)
        else:
            text = render_wave_notice(snapshot)
        sent = await safe_send_screen(
            bot,
            snapshot.group_id,
            text,
            parse_mode=None,
            # Wave starts are visible without a push; the battle start and the
            # result still notify, so the group is not spammed every wave.
            disable_notification=not final,
        )
        message_id = getattr(sent, "message_id", None) if sent is not None else None
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            _STAGE_NOTICE_RETRY_AFTER[retry_key] = time.monotonic() + ANNOUNCEMENT_RETRY_SECONDS
            logger.warning(
                "Invasion stage notice failed invasion_id=%s group_id=%s target=%s",
                snapshot.invasion_id,
                snapshot.group_id,
                target,
            )
            return False
        await _persist_presentation_stage(
            snapshot,
            expected_stage=snapshot.presentation_stage,
            new_stage=target,
        )
        _STAGE_NOTICE_RETRY_AFTER.pop(retry_key, None)
        # The pinned screen always shows the current state. Reload it so the
        # edit reflects anything committed since the snapshot was taken.
        refreshed = await _load_snapshot(snapshot.invasion_id) or snapshot
        await _edit_announcement(bot, refreshed)
        return True

    if changed:
        return await _edit_announcement(bot, snapshot)
    return False


class InvasionTelegramService:
    """Post-commit presentation hook used by the existing maintenance sweep."""

    @classmethod
    async def publish_maintenance_updates(
        cls,
        bot,
        changed_invasion_ids: Optional[list[int]] = None,
    ) -> int:
        """Present committed Invasion state after the transaction has committed.

        Four-wave battles: the battle screen is sent once, when the battle
        activates, and is pinned. Each wave start posts one silent notice, and
        the result posts one notifying notice. Each delivered stage is recorded
        by compare-and-set, so retries and restarts never repeat a notice.

        Legacy events keep the original behavior: a new announcement when the
        id is missing, and in-place edits when the lifecycle changed. Legacy
        SCHEDULED rows are never announced before they start.

        Telegram calls are capped per pass. The in-memory retry clocks only
        throttle Telegram; they are never a source of Invasion state.
        """
        if bot is None:
            return 0

        changed_ids = list(dict.fromkeys(int(item) for item in (changed_invasion_ids or [])))
        from sib.database.models import InvasionStatus

        wave_count_sq = (
            select(func.count(InvasionWave.id))
            .where(InvasionWave.invasion_id == Invasion.id)
            .scalar_subquery()
        )
        async with get_session() as session:
            pending_ids = list(
                (
                    await session.scalars(
                        select(Invasion.id)
                        .where(
                            Invasion.announcement_message_id.is_(None),
                            Invasion.status.in_(
                                (InvasionStatus.ACTIVE, InvasionStatus.FINISHED)
                            ),
                        )
                        .order_by(Invasion.created_at.asc(), Invasion.id.asc())
                        .limit(MAX_PENDING_INVASIONS_PER_SCAN)
                    )
                ).all()
            )
            staged_ids = list(
                (
                    await session.scalars(
                        select(Invasion.id)
                        .where(
                            Invasion.presentation_stage.is_not(None),
                            or_(
                                Invasion.status == InvasionStatus.ACTIVE,
                                and_(
                                    Invasion.status == InvasionStatus.FINISHED,
                                    Invasion.presentation_stage < wave_count_sq + 1,
                                ),
                            ),
                        )
                        .order_by(Invasion.id.asc())
                        .limit(MAX_PENDING_INVASIONS_PER_SCAN)
                    )
                ).all()
            )

        candidates = list(
            dict.fromkeys(
                changed_ids
                + [int(item) for item in pending_ids]
                + [int(item) for item in staged_ids]
            )
        )
        new_announcements = 0
        published = 0
        now_monotonic = time.monotonic()
        # Expired retry entries no longer protect a live pending message and
        # should not accumulate if an event is manually deleted.
        for retry_id, retry_after in tuple(_ANNOUNCEMENT_RETRY_AFTER.items()):
            if now_monotonic >= retry_after:
                _ANNOUNCEMENT_RETRY_AFTER.pop(retry_id, None)
        for retry_key, retry_after in tuple(_STAGE_NOTICE_RETRY_AFTER.items()):
            if now_monotonic >= retry_after:
                _STAGE_NOTICE_RETRY_AFTER.pop(retry_key, None)

        def _may_send(invasion_id: int, *, announcement: bool = True) -> bool:
            """Per-pass send cap; battle announcements also use their retry clock.

            Wave and result notices only count against the cap. Their own failure
            throttle is kept per stage, so a successful announcement never delays
            the next wave's notice.
            """
            nonlocal new_announcements
            if new_announcements >= MAX_NEW_ANNOUNCEMENTS_PER_CYCLE:
                return False
            if announcement:
                if now_monotonic < _ANNOUNCEMENT_RETRY_AFTER.get(invasion_id, 0.0):
                    return False
                _ANNOUNCEMENT_RETRY_AFTER[invasion_id] = (
                    now_monotonic + ANNOUNCEMENT_RETRY_SECONDS
                )
            new_announcements += 1
            return True

        for invasion_id in candidates:
            snapshot = None
            try:
                snapshot = await _load_snapshot(invasion_id)
                if snapshot is None:
                    continue
                if snapshot.presentation_stage is not None:
                    if await _publish_four_wave(
                        bot,
                        snapshot,
                        changed=invasion_id in changed_ids,
                        may_send=_may_send,
                    ):
                        published += 1
                    continue

                if snapshot.announcement_message_id is None:
                    if snapshot.status == InvasionStatus.SCHEDULED:
                        continue
                    if not _may_send(invasion_id):
                        continue
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
    async def pin_announcement(
        bot,
        snapshot: InvasionScreenSnapshot,
        message_id: int,
    ) -> bool:
        """Best-effort pin of an announcement whose id was just persisted.

        Presentation-only: never raises, never rolls anything back, and a
        failure is only logged. Callers use it after a replacement message
        (which deletes the previously pinned one) became the announcement.
        """
        return await _pin_announcement(bot, snapshot, message_id)

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
