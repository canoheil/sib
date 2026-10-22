"""Telegram entry point for «بوس سیبی» (Sib Kiss).

Validation happens here, before any database write, so a refused attempt
never creates a reward row or a cooldown. The domain (``SibKissService``)
re-checks ownership-sensitive rules under row locks and owns both rewards.

Failure policy: an unexpected database error is logged and answered with a
friendly retry message. ``get_session()`` rolls back before the reply, so no
partial reward survives.
"""

from aiogram import F, Router
from aiogram.types import Message

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.services.marriage_art import send_scene_reply
from sib.services.sib_kiss import (
    KISS_AWARDED,
    KISS_COOLDOWN,
    KISS_DUPLICATE,
    KISS_SCENE_KEY,
    SibKissError,
    SibKissService,
    format_kiss_cooldown_message,
    pick_kiss_message,
)
from sib.services.user import UserService

router = Router(name="sib_kiss")

# Persian Yeh (U+06CC) / Arabic Yeh (U+064A), same convention as the other
# Sib triggers. Anchored so ordinary sentences never match.
_YEH = "[\\u06cc\\u064a]"
KISS_TRIGGER_PATTERN = r"^\s*بوس\s+س" + _YEH + "ب" + _YEH + r"\s*[؟?]?\s*$"

KISS_PRIVATE_CHAT_TEXT = "😘 بوس سیبی فقط داخل گروه‌های سیب معتبره. 🍏"
KISS_REPLY_REQUIRED_TEXT = (
    "😘 برای بوس سیبی باید روی پیام کسی ریپلای بزنی و بعد «بوس سیبی» رو بنویسی. 🍏"
)
KISS_TARGET_NOT_A_USER_TEXT = (
    "😘 این پیام مال یه کاربر نیست؛ روی پیام یه کاربر واقعی ریپلای بزن. 🍏"
)
KISS_TARGET_IS_BOT_TEXT = "😘 ربات‌ها بوس سیبی نمی‌خوان؛ یه آدم واقعی رو انتخاب کن! 🍏"
KISS_SELF_TEXT = "😘 نمی‌تونی خودت رو بوس کنی! روی پیام یه نفر دیگه ریپلای بزن. 🍏"
KISS_GENERIC_FAILURE_TEXT = "😘 بوس سیبی ثبت نشد. یه لحظه‌ی دیگه دوباره امتحان کن. 🍏"


def _mention(user_id: int, first_name) -> str:
    """Markdown mention link with a Markdown-safe display name."""
    return f"[{safe_name(first_name, 'یک سیبی')}](tg://user?id={int(user_id)})"


@router.message(F.text.regexp(KISS_TRIGGER_PATTERN))
async def handle_sib_kiss(message: Message) -> None:
    """«بوس سیبی» as a REPLY to another user's message."""
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply(KISS_PRIVATE_CHAT_TEXT)
        return

    sender = message.from_user
    if sender is None:
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(KISS_REPLY_REQUIRED_TEXT)
        return

    target = getattr(reply, "from_user", None)
    if target is None:
        await message.reply(KISS_TARGET_NOT_A_USER_TEXT)
        return

    if getattr(target, "is_bot", False):
        await message.reply(KISS_TARGET_IS_BOT_TEXT)
        return

    if target.id == sender.id:
        await message.reply(KISS_SELF_TEXT)
        return

    message_key = f"{message.chat.id}:{message.message_id}"

    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, sender)
            await UserService.get_or_create_user(session, target)
            result = await SibKissService.kiss(
                session,
                sender_id=sender.id,
                target_id=target.id,
                message_key=message_key,
            )
    except SibKissError as error:
        logger.warning(f"sib kiss refused for {sender.id}->{target.id}: {error}")
        await message.reply(KISS_GENERIC_FAILURE_TEXT)
        return
    except Exception as error:
        # get_session() already rolled back; nothing partial is committed.
        logger.error(f"sib kiss failed for {sender.id}->{target.id}: {error}")
        await message.reply(KISS_GENERIC_FAILURE_TEXT)
        return

    # The transaction is closed: no row lock is held across the send below.
    if result.status == KISS_DUPLICATE:
        # This Telegram message was already processed; say nothing again.
        return
    if result.status == KISS_COOLDOWN:
        await message.reply(
            format_kiss_cooldown_message(result.remaining_seconds), parse_mode="Markdown"
        )
        return
    if result.status != KISS_AWARDED:  # pragma: no cover - defensive
        await message.reply(KISS_GENERIC_FAILURE_TEXT)
        return

    text = pick_kiss_message(
        _mention(sender.id, sender.first_name),
        _mention(target.id, target.first_name),
        result.amount,
    )
    await send_scene_reply(message, KISS_SCENE_KEY, text)
