"""Telegram adapter for Apple friendship requests and unfriend actions."""
import html
import math
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.database.models import AppleFriendship, FriendshipStatus, User
from sib.keyboards.friendship import (
    FRIEND_ACCEPT,
    FRIEND_REJECT,
    get_friend_list_keyboard,
    get_friend_request_keyboard,
    parse_friend_callback,
)
from sib.services.friendship import (
    FriendshipAlreadyExistsError,
    FriendshipError,
    FriendshipPendingError,
    FriendshipService,
)
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.user import UserService

router = Router(name="friendship")

_YEH = r"[\u06cc\u064a]"
_FRIEND_REQUEST = re.compile(rf"^\s*دوستی\s+س{_YEH}ب{_YEH}\s*[؟?]?\s*$")
_UNFRIEND = re.compile(r"^\s*قهر\s+س[\u06cc\u064a]ب[\u06cc\u064a]\s*[؟?]?\s*$")
_ASSET = Path(__file__).resolve().parent.parent / "assets" / "social" / "friend_request.jpg"
PAGE_SIZE = 10


def _card_text(sender_name: str, target_name: str, target_id: int) -> str:
    return (
        "🍏 <b>درخواست دوستی سیبی</b>\n\n"
        f"{html.escape(sender_name)} برای {html.escape(target_name)} درخواست دوستی فرستاده!\n\n"
        f"<a href=\"tg://user?id={int(target_id)}\">{html.escape(target_name)}</a> عزیز، اگه موافقی دکمهٔ قبول رو بزن."
    )


@router.message(F.text.regexp(_FRIEND_REQUEST))
async def handle_friend_request(message: Message) -> None:
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply("🍏 درخواست دوستی سیبی فقط داخل گروه‌های سیب انجام می‌شه.")
        return
    sender = message.from_user
    if sender is None:
        return
    reply = message.reply_to_message
    if reply is None:
        await message.reply("🍏 برای درخواست دوستی، روی پیام مخاطبت ریپلای کن و «دوستی سیبی» رو بنویس.")
        return
    target = getattr(reply, "from_user", None)
    if target is None:
        await message.reply("🍏 پیام ریپلای‌شده از یک کاربر واقعی نیست؛ روی پیام خودِ مخاطبت ریپلای بزن.")
        return

    async with get_session() as session:
        await UserService.get_or_create_user(session, sender)
        await UserService.get_or_create_user(session, target)
        try:
            request = await FriendshipService.create_request(session, sender.id, target.id)
            request_id = int(request.id)
        except FriendshipAlreadyExistsError as error:
            await session.rollback()
            await message.reply(str(error))
            return
        except FriendshipPendingError as error:
            await session.rollback()
            await message.reply(str(error))
            return
        except FriendshipError as error:
            await session.rollback()
            await message.reply(str(error))
            return
        except Exception as error:
            await session.rollback()
            logger.warning(f"Friend request creation failed: {error}")
            await message.reply("🍏 درخواست دوستی ثبت نشد؛ کمی بعد دوباره امتحان کن.")
            return

    caption = _card_text(sender.first_name or "یک سیبی", target.first_name or "یک سیبی", target.id)
    keyboard = get_friend_request_keyboard(request_id)
    if _ASSET.is_file():
        try:
            sent = await message.reply_photo(
                FSInputFile(_ASSET), caption=caption, reply_markup=keyboard, parse_mode="HTML"
            )
        except Exception as error:
            logger.warning(f"Friend request image delivery failed; falling back to text: {error}")
            try:
                sent = await message.reply(caption, reply_markup=keyboard, parse_mode="HTML")
            except Exception as fallback_error:
                logger.warning(f"Friend request card delivery failed: {fallback_error}")
                sent = None
    else:
        try:
            sent = await message.reply(caption, reply_markup=keyboard, parse_mode="HTML")
        except Exception as error:
            logger.warning(f"Friend request card delivery failed: {error}")
            sent = None
    if sent is None:
        async with get_session() as session:
            pending = await session.get(AppleFriendship, request_id)
            if pending is not None and pending.status == FriendshipStatus.PENDING:
                await session.delete(pending)
        await message.reply("🍏 نتونستم کارت درخواست دوستی رو بفرستم؛ درخواست ثبت نشد.")


@router.message(F.text.regexp(_UNFRIEND))
async def handle_unfriend(message: Message) -> None:
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply("💔 قهر سیبی فقط داخل گروه‌های سیب انجام می‌شه.")
        return
    sender = message.from_user
    if sender is None:
        return
    target = getattr(getattr(message, "reply_to_message", None), "from_user", None)
    if getattr(message, "reply_to_message", None) is None:
        await message.reply("💔 برای قهر سیبی، روی پیام دوستت ریپلای کن و «قهر سیبی» رو بنویس.")
        return
    if target is None:
        await message.reply("💔 پیام ریپلای‌شده از یک کاربر واقعی نیست؛ روی پیام دوستت ریپلای بزن.")
        return
    async with get_session() as session:
        await UserService.get_or_create_user(session, sender)
        await UserService.get_or_create_user(session, target)
        try:
            removed = await FriendshipService.unfriend(session, sender.id, target.id)
        except FriendshipError as error:
            await session.rollback()
            await message.reply(str(error))
            return
    if not removed:
        await message.reply("💔 شما با این کاربر دوست سیبی نیستید.")
        return
    first = safe_name(sender.first_name, "یک سیبی")
    second = safe_name(target.first_name, "یک سیبی")
    await message.reply(f"💔 {first} و {second} از هم قهر کردند.")


async def _resolve_friend_callback(callback: CallbackQuery, action: str) -> None:
    parsed = parse_friend_callback(callback.data)
    if parsed is None or parsed[0] != action:
        await safe_answer_callback(callback, "🍏 این دکمه معتبر نیست.", show_alert=True)
        return
    _parsed_action, request_id = parsed
    actor = callback.from_user
    if actor is None:
        return
    error = None
    sender_name = recipient_name = None
    try:
        async with get_session() as session:
            request = await session.get(AppleFriendship, request_id)
            if request is not None:
                sender = await session.get(User, request.user_id)
                recipient = await session.get(User, request.friend_id)
                sender_name = safe_name(sender.display_name if sender else None, "یک سیبی")
                recipient_name = safe_name(recipient.display_name if recipient else None, "یک سیبی")
            if action == FRIEND_ACCEPT:
                await FriendshipService.accept_request(session, request_id, actor.id)
            else:
                await FriendshipService.reject_request(session, request_id, actor.id)
    except FriendshipError as caught:
        error = str(caught)
    except Exception as caught:
        logger.warning(f"Friend request resolution failed: {caught}")
        error = "🍏 انجام درخواست ممکن نشد؛ کمی بعد دوباره امتحان کن."
    if error:
        await safe_answer_callback(callback, error, show_alert=True)
        return

    if action == FRIEND_ACCEPT:
        text = f"🍏 {sender_name} و {recipient_name} حالا دوست سیبی هم هستند!"
        await safe_answer_callback(callback, "درخواست دوستی قبول شد! 🍏")
    else:
        text = "درخواست دوستی رد شد."
        await safe_answer_callback(callback, "درخواست رد شد.")
    await safe_edit_callback_message(callback, callback.bot, text, reply_markup=None, parse_mode=None)


@router.callback_query(F.data.startswith(f"{FRIEND_ACCEPT}:"))
async def handle_friend_accept(callback: CallbackQuery) -> None:
    await _resolve_friend_callback(callback, FRIEND_ACCEPT)


@router.callback_query(F.data.startswith(f"{FRIEND_REJECT}:"))
async def handle_friend_reject(callback: CallbackQuery) -> None:
    await _resolve_friend_callback(callback, FRIEND_REJECT)


@router.callback_query(F.data == "profile:friends")
@router.callback_query(F.data.regexp(r"^profile:friends:[0-9]{1,6}$"))
async def handle_profile_friends(callback: CallbackQuery) -> None:
    actor = callback.from_user
    if actor is None:
        return
    data = callback.data or "profile:friends"
    try:
        page = max(0, int(data.rsplit(":", 1)[1])) if data.count(":") == 2 else 0
    except ValueError:
        page = 0
    async with get_session() as session:
        friends, total = await FriendshipService.list_friends(
            session, actor.id, offset=page * PAGE_SIZE, limit=PAGE_SIZE
        )
    page_count = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, page_count - 1)
    if page * PAGE_SIZE >= total and total:
        async with get_session() as session:
            friends, total = await FriendshipService.list_friends(
                session, actor.id, offset=page * PAGE_SIZE, limit=PAGE_SIZE
            )
    if total:
        first = page * PAGE_SIZE + 1
        last = min(first + len(friends) - 1, total)
        names = "\n".join(
            f"{fa_digits(first + index)}. {html.escape(friend.display_name or 'بازیکن بی‌نام')}"
            for index, friend in enumerate(friends)
        )
        owner_name = html.escape(actor.first_name or "شما")
        text = (
            f"🍏 <b>دوستان سیبی {owner_name}</b>\n\n{names}\n\n"
            f"صفحهٔ {fa_digits(page + 1)} از {fa_digits(page_count)} — "
            f"{fa_digits(first)} تا {fa_digits(last)} از {fa_digits(total)} نفر"
        )
    else:
        text = "🍏 هنوز دوست سیبی نداری. برای شروع، روی پیام یکی از بازیکن‌ها ریپلای کن و «دوستی سیبی» رو بنویس."
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback, callback.bot, text,
        reply_markup=get_friend_list_keyboard(page, page_count),
        parse_mode="HTML" if total else None,
    )
