"""Telegram entry point for the Daily Sib Treasure Chest («صندوقچه گنج روزانه»).

Two entry points, both text-only (no new artwork):

* ``/chest`` — open the chest screen (command pattern, like ``/garden``).
* «🎁 صندوقچه گنج روزانه» on the private main menu (``apple_chest``).

The screen shows whether today's chest is available, the exact reward of an
already-made claim and the next UTC reset. «باز کردن صندوقچه» (``daily_chest:
open``) is OWNER-SCOPED through the shared panel guard and delegates every
rule to ``DailyChestService``; the handler never rolls rewards itself.

Failure policy (same as Sib Hug): ``get_session()`` rolls the transaction
back before any error reply, so a failed open never leaves a consumed claim
or a half-delivered reward. Stale/duplicate presses are answered by the
already-claimed screen — never a second reward.
"""

from datetime import datetime, timezone
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import DailyChestClaim
from sib.keyboards.common import danger_button, primary_button
from sib.services.daily_chest import (
    ChestClaimResult,
    ChestConfigError,
    DailyChestError,
    DailyChestService,
    format_claimed_line,
    format_next_reset,
    format_open_result,
    next_reset_at,
)
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    PanelOwnershipService,
    resolve_personal_actor,
)
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.user import UserService

router = Router(name="daily_chest")

PANEL_TYPE = "chest"

CHEST_TITLE = "🎁 صندوقچه گنج روزانه سیب"
CHEST_POOL_LINE = (
    "🍎 سیب معمولی · 🌱 نهال · 🪙 سیب طلایی · "
    "💎 سیب الماسی · 🌑 سیب ابسیدینی · 🎴 کارت شخصیت"
)
CHEST_AVAILABLE_TEXT = (
    f"{CHEST_TITLE}\n\n"
    "هر روز یک بار بازش کن؛ یک جایزه‌ی تصادفی مال توئه! 🍏\n"
    f"{CHEST_POOL_LINE}\n\n"
    "صندوقچه‌ی امروز آماده‌ست. 👇"
)
CHEST_DISABLED_TEXT = f"{CHEST_TITLE}\n\nصندوقچه‌ی گنج فعلاً بسته‌ست. بعداً دوباره سر بزن! 🍏"
CHEST_CONFIG_ERROR_TEXT = "پیکربندی صندوقچه موقتاً مشکل داره؛ بعداً دوباره تلاش کن. 🍏"
CHEST_FAILURE_TEXT = "صندوقچه باز نشد. یه لحظه‌ی دیگه دوباره امتحان کن. 🍏"
OPEN_TOAST = "صندوقچه باز شد! 🎁"
ALREADY_TOAST = "این یکی رو امروز باز کردی! ⏳"
OPEN_BUTTON_TEXT = "🎁 باز کردن صندوقچه"
BACK_BUTTON_TEXT = "🏠 منوی اصلی"


def _keyboard(*, can_open: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_open:
        rows.append([primary_button(OPEN_BUTTON_TEXT, "daily_chest:open")])
    rows.append([danger_button(BACK_BUTTON_TEXT, "main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _claimed_screen(claim: DailyChestClaim, now: datetime) -> str:
    return (
        f"{CHEST_TITLE}\n\n"
        "صندوقچه‌ی امروز رو باز کردی! ✅\n"
        f"جایزه‌ی امروز: {format_claimed_line(claim)}\n\n"
        f"{format_next_reset(next_reset_at(now), now)}"
    )


def _result_screen(result: ChestClaimResult, now: datetime) -> str:
    return format_open_result(result, now)


async def _render_screen(
    user_id: int, session
) -> Tuple[str, InlineKeyboardMarkup]:
    """Chest screen for one player: available / claimed / disabled."""
    config = await DailyChestService.load_config(session)
    now = datetime.now(timezone.utc)
    if not config.enabled:
        return CHEST_DISABLED_TEXT, _keyboard(can_open=False)

    claim = await DailyChestService.get_today_claim(session, user_id, now=now)
    if claim is not None:
        return _claimed_screen(claim, now), _keyboard(can_open=False)
    return CHEST_AVAILABLE_TEXT, _keyboard(can_open=True)


async def _claim_panel_for(
    callback: CallbackQuery, user_id: int, prev_message_id: Optional[int]
) -> None:
    """Bind the chest panel message to its opener (DB, authoritative)."""
    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    if chat is None or not isinstance(msg_id, int):
        return
    async with get_session() as session:
        if isinstance(prev_message_id, int) and prev_message_id != msg_id:
            await PanelOwnershipService.move_claim(
                session,
                chat_id=chat.id,
                old_message_id=prev_message_id,
                new_message_id=msg_id,
                user_id=user_id,
                panel_type=PANEL_TYPE,
            )
        else:
            await PanelOwnershipService.claim(
                session,
                chat_id=chat.id,
                message_id=msg_id,
                user_id=user_id,
                panel_type=PANEL_TYPE,
            )


async def _panel_subject(callback: CallbackQuery) -> Optional[int]:
    """Authorized actor for a mutating chest press (defense in depth)."""
    user = callback.from_user
    if user is None:
        return None
    try:
        subject_id, _msg_id = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        try:
            await callback.answer(LOCKED_MESSAGE, show_alert=True)
        except Exception:  # pragma: no cover - defensive
            pass
        return None
    return subject_id


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
@router.message(Command("chest"))
async def cmd_chest(message: Message) -> None:
    """/chest — open the sender's daily chest screen."""
    user = message.from_user
    if user is None:
        return
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            text, kb = await _render_screen(user.id, session)
    except ChestConfigError:
        await message.reply(CHEST_CONFIG_ERROR_TEXT)
        return
    except Exception:
        logger.exception("daily chest screen failed")
        await message.reply(CHEST_FAILURE_TEXT)
        return
    await message.reply(text, reply_markup=kb)


@router.callback_query(F.data == "apple_chest")
async def handle_apple_chest(callback: CallbackQuery, bot: Bot) -> None:
    """Open the chest screen from the private main menu button."""
    user = callback.from_user
    if user is None:
        return
    prev_message_id = getattr(callback.message, "message_id", None)
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            text, kb = await _render_screen(user.id, session)
    except ChestConfigError:
        await safe_answer_callback(callback, CHEST_CONFIG_ERROR_TEXT, show_alert=True)
        return
    except Exception:
        logger.exception("daily chest screen failed")
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)
        return
    await safe_answer_callback(callback)
    if callback.message:
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
        await _claim_panel_for(callback, user.id, prev_message_id)


@router.callback_query(F.data == "daily_chest:open")
async def handle_chest_open(callback: CallbackQuery, bot: Bot) -> None:
    """Open today's chest (owner-scoped; exactly one reward per day)."""
    subject_id = await _panel_subject(callback)
    if subject_id is None:
        return

    user = callback.from_user
    now = datetime.now(timezone.utc)
    result: Optional[ChestClaimResult] = None
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            result = await DailyChestService.claim(session, subject_id, now=now)
            if result.status == "claimed":
                text = _result_screen(result, now)
            elif result.claim is not None:
                text = _claimed_screen(result.claim, now)
            else:  # pragma: no cover - claim() always yields one of the two
                text = CHEST_FAILURE_TEXT
    except ChestConfigError:
        await safe_answer_callback(callback, CHEST_CONFIG_ERROR_TEXT, show_alert=True)
        return
    except DailyChestError as error:
        logger.warning(f"daily chest refused for {subject_id}: {error}")
        await safe_answer_callback(callback, str(error), show_alert=True)
        return
    except Exception:
        logger.exception("daily chest open failed")
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)
        return

    claimed_now = result.status == "claimed"
    await safe_answer_callback(
        callback, OPEN_TOAST if claimed_now else ALREADY_TOAST, show_alert=not claimed_now
    )
    if callback.message:
        # After any open attempt the daily claim is consumed: the screen
        # keeps only navigation until the next UTC reset.
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=_keyboard(can_open=False)
        )
