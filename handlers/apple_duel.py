"""Group reply commands and recipient-only invitation callbacks."""

import re

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from sib.core.bidi import fix_bidi
from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.database.models import AppleDuel
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.apple_duel import AppleDuelService, DuelError
from sib.services.telegram_ui import safe_answer_callback
from sib.services.user import UserService
from sqlalchemy.exc import IntegrityError

router = Router(name="apple_duel")


def normalize_command(text):
    text = (text or "").translate(
        str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "\u200c": " "})
    )
    return " ".join(text.strip().rstrip(".!?؟،؛…").split())


class DuelCommand(Filter):
    async def __call__(self, message: Message):
        return normalize_command(message.text) in ("دوئل سیبی", "دوعل سیبی")


def invitation_keyboard(duel_id):
    """Invitation buttons: accept green, reject red, card swap blue."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button(text=text, callback_data=f"ad:{action}:{duel_id}")]
            for button, (action, text) in (
                (success_button, ("accept", "✅ قبول")),
                (danger_button, ("reject", "❌ رد")),
                (primary_button, ("change", "🔄 تعویض کارت")),
            )
        ]
    )


def validate_reply(message):
    if message.chat.type not in ("group", "supergroup"):
        raise DuelError("دوئل سیبی فقط در گروه برگزار می‌شود.")
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        raise DuelError("فرمان باید از طرف یک بازیکن واقعی باشد، نه فرستندهٔ ناشناس.")
    target = message.reply_to_message
    if not target:
        raise DuelError("روی پیام بازیکن دیگری ریپلای کن و بنویس «دوئل سیبی» ⚔️")
    if not target.from_user or target.from_user.is_bot or target.sender_chat:
        raise DuelError(
            "روی پیام یک بازیکن واقعی ریپلای کن؛ ربات و فرستندهٔ ناشناس معتبر نیستند."
        )
    if message.from_user.id == target.from_user.id:
        raise DuelError("نمی‌توانی خودت را به دوئل دعوت کنی.")
    return message.from_user, target.from_user


@router.message(DuelCommand())
async def command_duel(message: Message, bot: Bot):
    try:
        inviter, recipient = validate_reply(message)
        async with get_session() as session:
            await UserService.get_or_create_user(session, inviter)
            await UserService.get_or_create_user(session, recipient)
            duel = await AppleDuelService.create(
                session, message.chat.id, inviter.id, recipient.id
            )
    except (DuelError, IntegrityError) as exc:
        text = (
            str(exc)
            if isinstance(exc, DuelError)
            else "یکی از بازیکنان دعوت فعال دیگری دارد؛ دوباره تلاش کن."
        )
        await message.reply(text, parse_mode=None)
        return
    # Commit invitation BEFORE transport; failed send remains expirable in DB.
    try:
        sent = await message.reply(
            fix_bidi(
                f"⚔️ **{safe_name(inviter.full_name[:80])}** با "
                f"**{safe_name(recipient.full_name[:80])}** دوئل می‌خواد!\n"
                "کارتت رو انتخاب کن و «قبول» رو بزن."
            ),
            reply_markup=invitation_keyboard(duel.id),
            parse_mode="Markdown",
        )
        async with get_session() as session:
            stored = await session.get(AppleDuel, duel.id)
            stored.message_id = sent.message_id
    except Exception:
        logger.exception("Could not publish Apple Duel invitation %s", duel.id)


@router.callback_query(F.data.startswith("ad:"))
async def callback_duel(callback: CallbackQuery, bot: Bot):
    match = re.fullmatch(
        r"ad:(accept|reject|change):([0-9a-f]{32})", callback.data or ""
    )
    if (
        not match
        or not isinstance(callback.message, Message)
        or callback.from_user.is_bot
    ):
        await safe_answer_callback(callback, "دکمهٔ دعوت معتبر نیست.", show_alert=True)
        return
    action, duel_id = match.groups()
    try:
        async with get_session() as session:
            duel = await AppleDuelService.act(
                session,
                duel_id,
                callback.from_user.id,
                callback.message.chat.id,
                action,
                message_id=callback.message.message_id,
            )
        if action == "change" and duel.status == "PENDING":
            await open_existing_cards(callback, bot)
            return
        await safe_answer_callback(
            callback,
            {
                "ACCEPTED": "⚔️ دوئل شروع شد! صحنه‌ها در راه‌ان.",
                "COMPLETED": "🏆 این دوئل قبلاً تموم شده.",
                "REJECTED": "❌ دعوت رد شد.",
                "EXPIRED": "⌛ وقت دعوت تموم شده.",
            }.get(duel.status, "⏳ دعوت هنوز منتظر پاسخه."),
        )
        from sib.services.apple_duel_presentation import schedule_play

        schedule_play(bot, duel_id)
    except DuelError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
    except Exception:
        logger.exception("Apple Duel callback failed: %s", duel_id)
        await safe_answer_callback(
            callback, "اجرای درخواست موقتاً ممکن نیست؛ دوباره تلاش کن.", show_alert=True
        )


async def open_existing_cards(callback, bot):
    """Separate personal panel; original invitation/context is never edited."""
    from sib.handlers.start import handle_apple_cards
    from sib.services.panel import PanelOwnershipService

    sent = await bot.send_message(
        callback.message.chat.id,
        "🔄 کارت اصلی‌ت رو انتخاب کن و به دعوت برگرد.",
        parse_mode=None,
    )
    async with get_session() as session:
        await PanelOwnershipService.claim(
            session, sent.chat.id, sent.message_id, callback.from_user.id, "cards"
        )
    # Reuse the ENTIRE existing collection, pagination, card detail and selection flow.
    forwarded = callback.model_copy(update={"data": "apple_cards", "message": sent})
    await handle_apple_cards(forwarded, bot)
