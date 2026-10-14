"""Telegram group presentation and gameplay callbacks for Invasions.

Telegram supplies the authenticated player and originating Group context. The
Invasion service owns participant, Wave, action-availability, contribution,
and idempotency rules; this module only adapts callbacks and renders results.
"""

import hashlib
from types import SimpleNamespace
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from sib.core.logging import logger
from sib.database.connection import get_async_sessionmaker, get_session
from sib.database.models import (
    InvasionDefenseAction,
    InvasionParticipant,
    InvasionStatus,
)
from sib.keyboards.invasion import parse_invasion_callback
from sib.services.invasion import (
    InvasionAlreadyResolvedError,
    InvasionDefenseActionAlreadyUsedError,
    InvasionDefenseActionCode,
    InvasionDefenseActionConflictError,
    InvasionGroupMismatchError,
    InvasionInvalidDefenseActionError,
    InvasionJoinClosedError,
    InvasionInvalidWaveError,
    InvasionMainCardSnapshotError,
    InvasionNotActiveError,
    InvasionNotFoundError,
    InvasionNotParticipantError,
    InvasionService,
    InvasionStaleWaveError,
    InvasionStateError,
    InvasionUserNotFoundError,
    InvasionWaveError,
)
from sib.services.invasion_telegram import (
    InvasionScreenSnapshot,
    InvasionTelegramService,
    invasion_announcement_photo,
    invasion_status_keyboard,
    render_invasion_status,
)
from sib.services.maintenance.service import MaintenanceService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_screen
from sib.services.user import UserService

router = Router(name="invasion")

INVALID_CALLBACK_TEXT = "این دکمه معتبر نیست."
INVALID_GROUP_TEXT = "این دکمه در این گروه معتبر نیست."
STALE_ANNOUNCEMENT_TEXT = "این اعلان قدیمی است؛ وضعیت از اعلان تازه نمایش داده شد."
STALE_WAVE_TEXT = "این موج دیگر فعال نیست؛ وضعیت موج فعلی تازه شد و دفاعی ثبت نشد."
INVASION_UNAVAILABLE_TEXT = "این یورش دیگر در دسترس نیست."
INVASION_REWORK_TEXT = "⚔️ سیستم تهاجم موقتاً برای بازنویسی غیرفعال شده است."
JOIN_PENDING_TEXT = "⏳ درخواست پیوستن در حال بررسی است؛ نتیجه همین‌جا نمایش داده می‌شود."
STATUS_PENDING_TEXT = "⏳ وضعیت یورش در حال تازه‌شدن است."
BRACE_PENDING_TEXT = "⏳ دفاع در حال ثبت است."
JOIN_CONFIRMATION_FALLBACK_TEXT = (
    "⚔️ به یورش پیوستی. در هر موج، یک‌بار با 🛡 Brace دفاع کن؛ "
    "پیوستن به‌تنهایی دفاعی ثبت نمی‌کند."
)

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def _originating_group(callback: CallbackQuery) -> tuple[Optional[int], Optional[int]]:
    """Return chat/message ids only from the Telegram callback's source message."""
    message = callback.message
    if message is None:
        return None, None
    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", None) if chat is not None else None
    chat_type = getattr(chat_type, "value", chat_type)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    message_id = getattr(message, "message_id", None)
    if (
        chat_type not in ("group", "supergroup")
        or isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or message_id <= 0
    ):
        return None, None
    return chat_id, message_id


def _is_current_announcement(
    snapshot: InvasionScreenSnapshot,
    callback_message_id: int,
) -> bool:
    """Old messages stop being actionable after their persisted id changes."""
    return (
        snapshot.announcement_message_id is None
        or snapshot.announcement_message_id == callback_message_id
    )


def _brace_idempotency_key(callback_query_id: object) -> str:
    """Derive a retry-stable key from Telegram's callback-query identity.

    Telegram redelivery of one callback query preserves its query id. A fresh
    button press receives a distinct id. Hashing keeps the persisted key short
    and avoids relying on callback data or a user-supplied identifier.
    """
    if (
        not isinstance(callback_query_id, str)
        or not callback_query_id.strip()
        or len(callback_query_id.encode("utf-8")) > 512
    ):
        raise ValueError("callback query id is missing or invalid")
    digest = hashlib.sha256(callback_query_id.encode("utf-8")).hexdigest()
    return f"telegram:brace:{digest}"


async def _find_committed_join_participant(
    invasion_id: int,
    user_id: int,
    participant_id: int,
):
    """Read back a possibly committed join without reusing its failed session."""
    session_factory = get_async_sessionmaker()
    async with session_factory() as session:
        participant = await session.scalar(
            select(InvasionParticipant).where(
                InvasionParticipant.id == participant_id,
                InvasionParticipant.invasion_id == invasion_id,
                InvasionParticipant.user_id == user_id,
            )
        )
        if participant is None:
            await session.rollback()
            return None

        # Copy only the fields used by _join_confirmation before closing the
        # read-only session; a rollback/close must not expire this result.
        snapshot = SimpleNamespace(
            main_card_id_snapshot=participant.main_card_id_snapshot,
            character_name_fa_snapshot=participant.character_name_fa_snapshot,
            character_code_snapshot=participant.character_code_snapshot,
            card_level_snapshot=participant.card_level_snapshot,
            power_snapshot=participant.power_snapshot,
            speed_snapshot=participant.speed_snapshot,
            intelligence_snapshot=participant.intelligence_snapshot,
        )
        await session.rollback()
        return snapshot


async def _persist_rejected_join_maintenance(
    invasion_id: int,
    group_id: int,
):
    """Commit only due lifecycle maintenance after the join transaction rolls back.

    User registration/profile writes and the rejected participant attempt are
    deliberately excluded from this fresh transaction.
    """
    try:
        async with get_session() as session:
            invasion = await InvasionService.get(session, invasion_id)
            if invasion.group_id != group_id:
                return None
            result = await InvasionService.advance_battle(session, invasion_id)
        return result
    except InvasionNotFoundError:
        return None
    except Exception:
        logger.exception(
            "Could not persist Invasion maintenance after rejected join "
            "invasion_id=%s group_id=%s",
            invasion_id,
            group_id,
        )
        return None


async def _find_committed_defense_action(
    invasion_id: int,
    idempotency_key: str,
    result,
) -> bool:
    """Verify the exact action using its stable key in a new read-only session."""
    session_factory = get_async_sessionmaker()
    async with session_factory() as session:
        action = await session.scalar(
            select(InvasionDefenseAction).where(
                InvasionDefenseAction.id == result.action_id,
                InvasionDefenseAction.invasion_id == invasion_id,
                InvasionDefenseAction.idempotency_key == idempotency_key,
                InvasionDefenseAction.participant_id == result.participant_id,
                InvasionDefenseAction.wave_sequence == result.wave_sequence,
                InvasionDefenseAction.action_code == result.action_code,
            )
        )
        committed = action is not None
        await session.rollback()
        return committed


def _join_confirmation(participant) -> str:
    """Present exactly the Main Card snapshot returned by InvasionService."""
    # Joining is not defending: it registers you for this battle only. Each
    # wave then needs its own Brace (one per wave).
    lines = [
        "⚔️ به یورش پیوستی. در هر موج، یک‌بار با 🛡 Brace دفاع کن؛ "
        "پیوستن به‌تنهایی دفاعی ثبت نمی‌کند."
    ]
    if participant.main_card_id_snapshot is None:
        lines.append(
            "🎴 کارت اصلی: انتخاب نشده؛ بدون کارت اصلی به جمع مدافعان پیوستی."
        )
        return "\n".join(lines)

    character_name = (
        participant.character_name_fa_snapshot
        or participant.character_code_snapshot
        or "شخصیت نامشخص"
    )
    lines.extend(
        [
            f"🎴 کارت اصلی: {character_name}",
            f"⭐ سطح: {_fa(participant.card_level_snapshot)}",
            (
                f"💪 قدرت: {_fa(participant.power_snapshot)} | "
                f"⚡ سرعت: {_fa(participant.speed_snapshot)} | "
                f"🧠 هوش: {_fa(participant.intelligence_snapshot)}"
            ),
        ]
    )
    return "\n".join(lines)


def _join_error_text(error: Exception, persisted_status: Optional[str]) -> str:
    if isinstance(error, InvasionJoinClosedError):
        if persisted_status == InvasionStatus.SCHEDULED:
            return "⏳ دفاع هنوز آغاز نشده؛ بعد از شروع یورش دوباره تلاش کن."
        if persisted_status == InvasionStatus.FINISHED:
            return "🏁 دفاع این یورش بسته شده و دیگر امکان پیوستن نیست."
        return "🛡 در حال حاضر امکان پیوستن به این دفاع نیست."
    if isinstance(error, InvasionMainCardSnapshotError):
        return "🎴 وضعیت کارت اصلی‌ات تغییر کرد؛ یک‌بار دیگر پیوستن را بزن."
    if isinstance(error, InvasionUserNotFoundError):
        return "حساب بازیکن پیدا نشد؛ دوباره از دکمه پیوستن استفاده کن."
    if isinstance(error, InvasionGroupMismatchError):
        return INVALID_GROUP_TEXT
    if isinstance(error, InvasionStateError):
        return "این دفاع دیگر برای پیوستن در دسترس نیست."
    return "ثبت پیوستن انجام نشد؛ کمی بعد دوباره تلاش کن."


async def _show_group_notice(bot: Bot, group_id: int, text: str) -> None:
    """Send post-transaction feedback best-effort in the originating Group."""
    try:
        await InvasionTelegramService.send_group_notice(bot, group_id, text)
    except Exception:
        logger.warning(
            "Could not send Invasion Group feedback group_id=%s",
            group_id,
            exc_info=True,
        )


async def _refresh_status_message(
    bot: Bot,
    snapshot: InvasionScreenSnapshot,
    *,
    fallback_message_id: int,
) -> bool:
    """Refresh the persisted announcement and CAS-persist any replacement id."""
    target_message_id = snapshot.announcement_message_id or fallback_message_id
    new_message_id = await safe_edit_screen(
        bot,
        snapshot.group_id,
        target_message_id,
        render_invasion_status(snapshot),
        photo=invasion_announcement_photo(),
        reply_markup=invasion_status_keyboard(snapshot),
        parse_mode=None,
        current_is_photo=None,
    )
    if new_message_id is None:
        return False

    expected_message_id = snapshot.announcement_message_id
    if new_message_id != expected_message_id:
        saved = await InvasionTelegramService.persist_replacement_id(
            snapshot,
            new_message_id,
            expected_message_id=expected_message_id,
        )
        if not saved:
            logger.warning(
                "Invasion status refresh lost announcement-id CAS invasion_id=%s "
                "group_id=%s expected=%s returned=%s",
                snapshot.invasion_id,
                snapshot.group_id,
                expected_message_id,
                new_message_id,
            )
        else:
            # The replacement deleted the previously pinned message; keep the
            # announcement pinned. Best-effort: a pin failure never fails the
            # refresh that already succeeded.
            await InvasionTelegramService.pin_announcement(
                bot,
                snapshot,
                new_message_id,
            )
    return True


async def _load_viewer_snapshot(
    invasion_id: int,
    *,
    viewer_user_id: int,
    group_id: int,
    fallback_message_id: int,
    bot: Bot,
) -> Optional[InvasionScreenSnapshot]:
    try:
        snapshot = await InvasionTelegramService.load_snapshot(
            invasion_id,
            viewer_user_id=viewer_user_id,
        )
    except Exception:
        logger.exception(
            "Could not load Invasion status invasion_id=%s group_id=%s",
            invasion_id,
            group_id,
        )
        await _show_group_notice(
            bot,
            group_id,
            "وضعیت یورش فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.",
        )
        return None

    if snapshot is None:
        await _show_group_notice(bot, group_id, INVASION_UNAVAILABLE_TEXT)
        return None
    if snapshot.group_id != group_id:
        await _show_group_notice(bot, group_id, INVALID_GROUP_TEXT)
        return None
    return snapshot


async def _handle_join(
    callback: CallbackQuery,
    bot: Bot,
    *,
    invasion_id: int,
    group_id: int,
    callback_message_id: int,
    actor,
) -> None:
    persisted_status: Optional[str] = None
    participant = None
    pending_participant = None
    pending_participant_id: Optional[int] = None
    join_attempted = False
    lifecycle_changed = False
    try:
        async with get_session() as session:
            invasion = await InvasionService.get(session, invasion_id)
            persisted_status = invasion.status
            if invasion.group_id != group_id:
                result_text = INVALID_GROUP_TEXT
            elif not _is_current_announcement(invasion, callback_message_id):
                result_text = STALE_ANNOUNCEMENT_TEXT
            else:
                # Identity comes from this authenticated Telegram update, never
                # from callback data or the public announcement message.
                await UserService.get_or_create_user(session, actor)
                join_attempted = True
                pending_participant = await InvasionService.join(
                    session,
                    invasion_id,
                    actor.id,
                    expected_group_id=group_id,
                )
                pending_participant_id = int(pending_participant.id)
        # Only expose a participant to the presenter after get_session() has
        # committed successfully, unless a later authoritative read proves the
        # commit succeeded despite an acknowledgement error.
        participant = pending_participant
    except InvasionNotFoundError:
        result_text = INVASION_UNAVAILABLE_TEXT
    except (
        InvasionJoinClosedError,
        InvasionGroupMismatchError,
        InvasionMainCardSnapshotError,
        InvasionStateError,
        InvasionUserNotFoundError,
    ) as error:
        if join_attempted and isinstance(
            error,
            (
                InvasionJoinClosedError,
                InvasionMainCardSnapshotError,
                InvasionUserNotFoundError,
            ),
        ):
            # join() may have advanced a due timeline before rejecting the
            # participant. The join transaction has now rolled back, including
            # User/Progression/profile writes, so persist only maintenance in a
            # fresh transaction.
            advance = await _persist_rejected_join_maintenance(invasion_id, group_id)
            if advance is not None:
                persisted_status = advance.status
                lifecycle_changed = advance.changed
        result_text = _join_error_text(error, persisted_status)
    except Exception:
        logger.exception(
            "Invasion join handler failed invasion_id=%s group_id=%s user_id=%s",
            invasion_id,
            group_id,
            actor.id,
        )
        if pending_participant_id is not None:
            try:
                participant = await _find_committed_join_participant(
                    invasion_id,
                    actor.id,
                    pending_participant_id,
                )
            except Exception:
                # Without a successful fresh read, retain the conservative
                # failure response; never retry the non-idempotent join write.
                logger.exception(
                    "Could not reconcile ambiguous Invasion join commit "
                    "invasion_id=%s group_id=%s user_id=%s",
                    invasion_id,
                    group_id,
                    actor.id,
                )
            if participant is not None:
                logger.warning(
                    "Invasion join participant persisted after commit "
                    "acknowledgement error; treating as accepted "
                    "invasion_id=%s group_id=%s user_id=%s participant_id=%s",
                    invasion_id,
                    group_id,
                    actor.id,
                    pending_participant_id,
                )
        result_text = "ثبت پیوستن انجام نشد؛ کمی بعد دوباره تلاش کن."

    if lifecycle_changed:
        await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
    if participant is not None:
        try:
            # Formatting is presentation-only. It happens after commit so a
            # formatting defect cannot roll back a valid participant row.
            result_text = _join_confirmation(participant)
        except Exception:
            logger.exception(
                "Could not format committed Invasion join confirmation "
                "invasion_id=%s group_id=%s user_id=%s",
                invasion_id,
                group_id,
                actor.id,
            )
            result_text = JOIN_CONFIRMATION_FALLBACK_TEXT

    # get_session() committed (or rolled back) before Telegram feedback.
    await _show_group_notice(bot, group_id, result_text)


async def _handle_status(
    callback: CallbackQuery,
    bot: Bot,
    *,
    invasion_id: int,
    group_id: int,
    callback_message_id: int,
    actor,
) -> None:
    snapshot = await _load_viewer_snapshot(
        invasion_id,
        viewer_user_id=actor.id,
        group_id=group_id,
        fallback_message_id=callback_message_id,
        bot=bot,
    )
    if snapshot is None:
        return

    if not _is_current_announcement(snapshot, callback_message_id):
        await _refresh_status_message(
            bot,
            snapshot,
            fallback_message_id=callback_message_id,
        )
        await _show_group_notice(bot, group_id, STALE_ANNOUNCEMENT_TEXT)
        return

    if not await _refresh_status_message(
        bot,
        snapshot,
        fallback_message_id=callback_message_id,
    ):
        await _show_group_notice(
            bot,
            group_id,
            "وضعیت یورش ثبت‌شده است، اما نتونستم این اعلان را تازه کنم.",
        )


async def _refresh_after_action_error(
    bot: Bot,
    *,
    invasion_id: int,
    user_id: int,
    group_id: int,
    callback_message_id: int,
) -> Optional[InvasionScreenSnapshot]:
    snapshot = await _load_viewer_snapshot(
        invasion_id,
        viewer_user_id=user_id,
        group_id=group_id,
        fallback_message_id=callback_message_id,
        bot=bot,
    )
    if snapshot is not None:
        await _refresh_status_message(
            bot,
            snapshot,
            fallback_message_id=callback_message_id,
        )
    return snapshot


def _brace_result_text(result) -> str:
    heading = (
        "↩️ این درخواست قبلاً ثبت شده است."
        if result.duplicate
        else "🛡 دفاع پذیرفته شد."
    )
    return "\n".join(
        [
            heading,
            f"➕ سهم این کنش: {_fa(result.contribution_amount)}",
            f"👤 سهم کل شما: {_fa(result.participant_total_after)}",
            (
                f"🌊 دفاع موج {_fa(result.wave_sequence)}: "
                f"{_fa(result.wave_total_after)}"
            ),
        ]
    )


async def _handle_brace(
    callback: CallbackQuery,
    bot: Bot,
    *,
    invasion_id: int,
    wave_sequence: int,
    group_id: int,
    callback_message_id: int,
    actor,
) -> None:
    snapshot = await _load_viewer_snapshot(
        invasion_id,
        viewer_user_id=actor.id,
        group_id=group_id,
        fallback_message_id=callback_message_id,
        bot=bot,
    )
    if snapshot is None:
        return
    if not _is_current_announcement(snapshot, callback_message_id):
        await _refresh_status_message(
            bot,
            snapshot,
            fallback_message_id=callback_message_id,
        )
        await _show_group_notice(bot, group_id, STALE_ANNOUNCEMENT_TEXT)
        return

    timeline_error = None
    result = None
    idempotency_key = None
    try:
        # callback.id is the stable source for a Telegram delivery retry. The
        # Wave/Event/user payload is still validated against persisted state by
        # the domain API, and user identity comes only from callback.from_user.
        idempotency_key = _brace_idempotency_key(callback.id)
        async with get_session() as session:
            try:
                result = await InvasionService.perform_defense_action(
                    session,
                    invasion_id,
                    actor.id,
                    expected_group_id=group_id,
                    wave_sequence=wave_sequence,
                    action_code=InvasionDefenseActionCode.BRACE,
                    idempotency_key=idempotency_key,
                )
            except (
                InvasionAlreadyResolvedError,
                InvasionInvalidWaveError,
                InvasionNotActiveError,
                InvasionNotParticipantError,
                InvasionStaleWaveError,
            ) as error:
                # These checks happen after perform_defense_action has brought
                # the timeline up to date and before it writes an action. Keep
                # that authoritative maintenance even though this callback is
                # stale, late, or from a nonparticipant. Database/action write
                # errors remain outside this list and still roll the session
                # back as a unit.
                timeline_error = error
        if timeline_error is not None:
            raise timeline_error
    except InvasionNotFoundError:
        await _show_group_notice(bot, group_id, INVASION_UNAVAILABLE_TEXT)
        return
    except InvasionGroupMismatchError:
        await _show_group_notice(bot, group_id, INVALID_GROUP_TEXT)
        return
    except InvasionNotParticipantError:
        latest = await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
        if latest is not None and latest.status == InvasionStatus.FINISHED:
            return
        await _show_group_notice(
            bot,
            group_id,
            "برای دفاع از این یورش، اول به جمع مدافعان بپیوند.",
        )
        return
    except InvasionDefenseActionAlreadyUsedError:
        await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
        await _show_group_notice(
            bot,
            group_id,
            "✅ Brace را در این موج قبلاً انجام دادی؛ در موج بعدی دوباره در دسترس است.",
        )
        return
    except InvasionWaveError:
        await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
        await _show_group_notice(bot, group_id, STALE_WAVE_TEXT)
        return
    except InvasionAlreadyResolvedError:
        await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
        return
    except InvasionNotActiveError:
        await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
        await _show_group_notice(
            bot,
            group_id,
            "این یورش فعال نیست و دفاع تازه‌ای ثبت نشد.",
        )
        return
    except InvasionDefenseActionConflictError:
        await _show_group_notice(
            bot,
            group_id,
            "این درخواست با کنش دیگری تداخل دارد؛ وضعیت را تازه کن و دوباره تلاش کن.",
        )
        return
    except InvasionInvalidDefenseActionError:
        await _show_group_notice(bot, group_id, INVALID_CALLBACK_TEXT)
        return
    except InvasionStateError:
        await _refresh_after_action_error(
            bot,
            invasion_id=invasion_id,
            user_id=actor.id,
            group_id=group_id,
            callback_message_id=callback_message_id,
        )
        await _show_group_notice(
            bot,
            group_id,
            "این دفاع دیگر برای انجام کنش در دسترس نیست.",
        )
        return
    except ValueError:
        await _show_group_notice(bot, group_id, INVALID_CALLBACK_TEXT)
        return
    except Exception:
        logger.exception(
            "Invasion brace handler failed invasion_id=%s group_id=%s user_id=%s "
            "wave_sequence=%s",
            invasion_id,
            group_id,
            actor.id,
            wave_sequence,
        )
        committed = False
        if result is not None and idempotency_key is not None:
            try:
                committed = await _find_committed_defense_action(
                    invasion_id,
                    idempotency_key,
                    result,
                )
            except Exception:
                # The outcome remains ambiguous if the authoritative read is
                # unavailable. Do not repeat a non-idempotent write.
                logger.exception(
                    "Could not reconcile ambiguous Invasion brace commit "
                    "invasion_id=%s group_id=%s user_id=%s wave_sequence=%s",
                    invasion_id,
                    group_id,
                    actor.id,
                    wave_sequence,
                )
        if not committed:
            await _show_group_notice(
                bot,
                group_id,
                "ثبت دفاع انجام نشد؛ کمی بعد دوباره تلاش کن.",
            )
            return
        logger.warning(
            "Invasion brace action persisted after commit acknowledgement error; "
            "treating as accepted invasion_id=%s group_id=%s user_id=%s "
            "wave_sequence=%s action_id=%s",
            invasion_id,
            group_id,
            actor.id,
            wave_sequence,
            result.action_id,
        )

    # Domain commit completes before message edit or send. A Telegram outage
    # can delay feedback, but can never undo the accepted defense action.
    await _show_group_notice(bot, group_id, _brace_result_text(result))
    try:
        refreshed = await InvasionTelegramService.load_snapshot(
            invasion_id,
            viewer_user_id=actor.id,
        )
        if refreshed is not None and refreshed.group_id == group_id:
            await _refresh_status_message(
                bot,
                refreshed,
                fallback_message_id=callback_message_id,
            )
    except Exception:
        logger.exception(
            "Could not refresh Invasion after brace invasion_id=%s group_id=%s",
            invasion_id,
            group_id,
        )


@router.callback_query(F.data.startswith("invasion:"))
async def handle_invasion_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Handle Join, brace, and status callbacks using authenticated update data."""
    if not await MaintenanceService._invasion_enabled():
        # [INVASION-REWORK] guarded — see docs/invasion-rework/
        await safe_answer_callback(callback, INVASION_REWORK_TEXT, show_alert=True)
        return

    parsed = parse_invasion_callback(callback.data)
    if parsed is None:
        await safe_answer_callback(callback, INVALID_CALLBACK_TEXT, show_alert=True)
        return

    action, invasion_id = parsed[:2]
    wave_sequence = parsed[2] if len(parsed) == 3 else None
    group_id, callback_message_id = _originating_group(callback)
    actor = callback.from_user
    if group_id is None or callback_message_id is None or actor is None:
        await safe_answer_callback(
            callback,
            "این دکمه فقط در گروه مربوط به یورش معتبر است.",
            show_alert=True,
        )
        return
    if isinstance(actor.id, bool) or not isinstance(actor.id, int) or actor.id <= 0:
        await safe_answer_callback(callback, INVALID_CALLBACK_TEXT, show_alert=True)
        return

    pending_text = {
        "join": JOIN_PENDING_TEXT,
        "status": STATUS_PENDING_TEXT,
        "brace": BRACE_PENDING_TEXT,
    }[action]
    await safe_answer_callback(callback, pending_text)

    if action == "join":
        await _handle_join(
            callback,
            bot,
            invasion_id=invasion_id,
            group_id=group_id,
            callback_message_id=callback_message_id,
            actor=actor,
        )
        return
    if action == "brace":
        # The parser guarantees a positive Wave sequence for brace callbacks.
        if wave_sequence is None:  # pragma: no cover - defensive parser guard
            await _show_group_notice(bot, group_id, INVALID_CALLBACK_TEXT)
            return
        await _handle_brace(
            callback,
            bot,
            invasion_id=invasion_id,
            wave_sequence=wave_sequence,
            group_id=group_id,
            callback_message_id=callback_message_id,
            actor=actor,
        )
        return

    await _handle_status(
        callback,
        bot,
        invasion_id=invasion_id,
        group_id=group_id,
        callback_message_id=callback_message_id,
        actor=actor,
    )


__all__ = [
    "_brace_idempotency_key",
    "handle_invasion_callback",
    "router",
]
