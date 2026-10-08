"""Telegram entry point for «بغل سیبی» (Sib Hug).

Validation happens here, before any database write, so a refused attempt
never creates a reward row or a cooldown. The domain (``SibHugService``)
re-checks the ownership-sensitive rules under row locks and owns both
rewards. The handler never computes amounts or cooldowns itself.

Failure policy: an unexpected database error is logged and answered with a
friendly retry message. The transaction is rolled back by ``get_session()``
before the reply, so no partial reward survives.
"""

from aiogram import F, Router
from aiogram.types import Message

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.services.marriage_art import send_scene_reply
from sib.services.sib_hug import (
    HUG_AWARDED,
    HUG_COOLDOWN,
    HUG_DUPLICATE,
    HUG_SCENE_KEY,
    SibHugError,
    SibHugService,
    format_hug_cooldown_message,
    pick_hug_message,
)
from sib.services.user import UserService

router = Router(name="sib_hug")

# Persian Yeh (U+06CC) / Arabic Yeh (U+064A), same convention as the
# marriage and flower triggers. Anchored so ordinary sentences never match.
_YEH = "[\\u06cc\\u064a]"
HUG_TRIGGER_PATTERN = r"^\s*بغل\s+س" + _YEH + "ب" + _YEH + r"\s*[؟?]?\s*$"

HUG_PRIVATE_CHAT_TEXT = "🤗 بغل سیبی فقط داخل گروه‌های سیب معتبره. 🍏"
HUG_REPLY_REQUIRED_TEXT = (
    "🤗 برای بغل سیبی باید روی پیام کسی ریپلای بزنی و بعد «بغل سیبی» رو بنویسی. 🍏"
)
HUG_TARGET_NOT_A_USER_TEXT = (
    "🤗 این پیام مال یه کاربر نیست؛ روی پیام یه کاربر واقعی ریپلای بزن. 🍏"
)
HUG_TARGET_IS_BOT_TEXT = "🤗 ربات‌ها بغل سیبی نمی‌خوان؛ یه آدم واقعی رو بغل کن! 🍏"
HUG_SELF_TEXT = "🤗 نمی‌تونی خودت رو بغل کنی! روی پیام یه نفر دیگه ریپلای بزن. 🍏"
HUG_GENERIC_FAILURE_TEXT = "🤗 بغل سیبی ثبت نشد. یه لحظه‌ی دیگه دوباره امتحان کن. 🍏"


def _mention(user_id: int, first_name) -> str:
    """Markdown mention link with a Markdown-safe display name."""
    return f"[{safe_name(first_name, 'یک سیبی')}](tg://user?id={int(user_id)})"


@router.message(F.text.regexp(HUG_TRIGGER_PATTERN))
async def handle_sib_hug(message: Message) -> None:
    """«بغل سیبی» as a REPLY to another user's message."""
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply(HUG_PRIVATE_CHAT_TEXT)
        return

    sender = message.from_user
    if sender is None:
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(HUG_REPLY_REQUIRED_TEXT)
        return

    target = getattr(reply, "from_user", None)
    if target is None:
        # Channel posts and anonymous admins have no user identity.
        await message.reply(HUG_TARGET_NOT_A_USER_TEXT)
        return

    if getattr(target, "is_bot", False):
        await message.reply(HUG_TARGET_IS_BOT_TEXT)
        return

    if target.id == sender.id:
        await message.reply(HUG_SELF_TEXT)
        return

    message_key = f"{message.chat.id}:{message.message_id}"

    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, sender)
            await UserService.get_or_create_user(session, target)
            result = await SibHugService.hug(
                session,
                sender_id=sender.id,
                target_id=target.id,
                message_key=message_key,
            )
    except SibHugError as error:
        logger.warning(f"sib hug refused for {sender.id}->{target.id}: {error}")
        await message.reply(HUG_GENERIC_FAILURE_TEXT)
        return
    except Exception as error:
        # get_session() already rolled back; nothing partial is committed.
        logger.error(f"sib hug failed for {sender.id}->{target.id}: {error}")
        await message.reply(HUG_GENERIC_FAILURE_TEXT)
        return

    # The transaction is closed: no row lock is held across the send below.
    if result.status == HUG_DUPLICATE:
        # This Telegram message was already processed; say nothing again.
        return
    if result.status == HUG_COOLDOWN:
        await message.reply(format_hug_cooldown_message(result.remaining_seconds))
        return
    if result.status != HUG_AWARDED:  # pragma: no cover - defensive
        await message.reply(HUG_GENERIC_FAILURE_TEXT)
        return

    text = pick_hug_message(
        _mention(sender.id, sender.first_name),
        _mention(target.id, target.first_name),
        result.amount,
    )
    await send_scene_reply(message, HUG_SCENE_KEY, text)
