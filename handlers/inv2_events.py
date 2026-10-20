"""IV3 chest claim: one public group button, first N winners, never twice.

The callback is deliberately *not* owner-scoped: like the two join buttons on
the invasion announcement, every member of the group must be able to press it.
Ownership is proved by the message (the event row stores the group and the
message id it was posted to), so a forwarded message can never be claimed in
another group.

All state lives in ``InvasionEventScheduler.claim_loot``: the event row lock,
a compare-and-swap counter and two unique constraints decide who is third.
This handler only parses the callback, commits, and answers.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.exc import IntegrityError

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.invasion_v2 import presentation
from sib.services.invasion_v2.event_scheduler import claim_loot, events_enabled
from sib.services.maintenance.service import MaintenanceService

router = Router(name="inv2_events")

PREFIX = "inv2ev:loot:"


async def _answer(callback: CallbackQuery, text: str, *, alert: bool = False) -> None:
    group_id = getattr(getattr(callback.message, "chat", None), "id", None)
    try:
        if group_id is not None:
            await presentation._telegram(
                group_id, callback.answer, text=text, show_alert=alert
            )
        else:
            await callback.answer(text=text, show_alert=alert)
    except Exception:
        logger.debug("inv2 chest callback answer failed", exc_info=True)


@router.callback_query(F.data.startswith(PREFIX))
async def handle_inv2_loot(callback: CallbackQuery, bot) -> None:
    try:
        if not await MaintenanceService._invasion_enabled():
            await _answer(callback, "⚔️ سامانهٔ یورش موقتاً غیرفعال است.", alert=True)
            return
        if not callback.from_user or callback.message is None:
            await _answer(callback, "این صندوق پیدا نشد.", alert=True)
            return
        suffix = (callback.data or "").removeprefix(PREFIX)
        if not suffix.isdecimal():
            await _answer(callback, "این صندوق پیدا نشد.", alert=True)
            return
        event_id = int(suffix)
        group_id = getattr(callback.message.chat, "id", None)
        message_id = getattr(callback.message, "message_id", None)

        async with get_session() as session:
            if not await events_enabled(session):
                await _answer(callback, "رویدادهای یورش غیرفعال است.", alert=True)
                return
            result = await claim_loot(
                session,
                event_id,
                callback.from_user.id,
                group_id=group_id,
                message_id=message_id,
            )
        if result.ok:
            logger.info(
                "inv2 chest claimed event=%s user=%s slot=%s/%s",
                event_id, callback.from_user.id, result.claimed, result.max_claims,
            )
        # Answers are private alerts; the shared caption is the group-facing
        # announcement, so a refused click never spams the chat.
        await _answer(callback, result.message_fa, alert=True)
        if result.ok:
            await presentation.refresh_loot_caption(bot, event_id)
    except IntegrityError:
        # A concurrent claim won the unique constraint; nothing was granted
        # twice because the whole transaction rolled back.
        logger.info("inv2 chest duplicate claim rejected event=%s", callback.data)
        await _answer(callback, "قبلاً از این صندوق برداشته‌اید.", alert=True)
    except Exception:
        logger.exception("inv2 chest claim failed data=%s", callback.data)
        await _answer(callback, "برداشتن جایزه ممکن نشد؛ کمی بعد دوباره تلاش کنید.", alert=True)
