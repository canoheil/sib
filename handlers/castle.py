"""Group-only Apple Castle screens. All mutations commit before Telegram delivery."""

from html import escape
from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import CastleDonation, User
from sib.services.castle_service import CastleService, CastleError, THRESHOLDS
from sib.services.castle_art import castle_image
from sib.services.sib_claim import fa_digits
from sib.services.user import UserService
from sib.services.telegram_ui import safe_answer_callback
from sib.keyboards.common import danger_button, primary_button, success_button

router = Router(name="castle")
GROUPS = {"group", "supergroup"}


def keyboard(group_id, donate=False):
    if donate:
        buttons = [
            success_button("۱۰۰ 🍏", f"castle:100:{group_id}"),
            success_button("۵۰۰ 🍏", f"castle:500:{group_id}"),
            success_button("حداکثر", f"castle:max:{group_id}"),
            danger_button("🔙 بازگشت", f"castle:view:{group_id}"),
        ]
    else:
        buttons = [
            success_button("🍏 اهدای سیب", f"castle:donate:{group_id}"),
            primary_button("🎁 دریافت درآمد", f"castle:income:{group_id}"),
            primary_button("🏆 برترین اهداکنندگان", f"castle:top:{group_id}"),
        ]
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def format_castle_caption(level, total_donated):
    """Render the Apple Castle status in Persian with Persian numerals."""
    next_threshold = (
        THRESHOLDS[level] if level < len(THRESHOLDS) else THRESHOLDS[-1]
    )
    buff_percent = level * 2
    income = level * 2
    return (
        "🏰 قلعه سیبی\n\n"
        f"📊 سطح قلعه: {fa_digits(level)} از ۱۰\n\n"
        f"🍏 سیب اهدا شده: {fa_digits(total_donated)} / {fa_digits(next_threshold)}\n\n"
        f"⚔️ قدرت حمله در یورش: +{fa_digits(buff_percent)}٪\n\n"
        f"🎁 درآمد روزانه: {fa_digits(income)} سیب\n\n"
        "⚠️ سقف اهدا: روزانه ۵۰۰ سیب (برای هر بازیکن)\n\n"
        "ℹ️ اهدای سیب بازگشت‌پذیر نیست. درآمد روزانه فقط از یک قلعه دریافت می‌شود."
    )


async def show_castle(message, group_id, *, donation_menu=False):
    async with get_session() as session:
        castle = await CastleService.get_or_create_castle(session, group_id)
        level, total = castle.level, castle.total_donated
    caption = format_castle_caption(level, total)
    markup = keyboard(group_id, donation_menu)
    image = castle_image(level)
    if image:
        try:
            await message.answer_photo(
                FSInputFile(image), caption=caption, reply_markup=markup
            )
            return
        except Exception:
            logger.warning("Castle photo delivery failed; using text", exc_info=True)
    await message.answer(caption, reply_markup=markup)


@router.message(F.chat.type.in_(GROUPS), F.text == "قلعه سیبی")
async def castle_entry(message: Message):
    try:
        await show_castle(message, message.chat.id)
    except CastleError as exc:
        await message.answer(str(exc))


@router.callback_query(F.data.startswith("castle:"))
async def castle_callback(callback: CallbackQuery):
    message = callback.message
    parts = (callback.data or "").split(":")
    if (
        not isinstance(message, Message)
        or message.chat.type not in GROUPS
        or len(parts) != 3
        or parts[2] != str(message.chat.id)
        or callback.from_user.is_bot
    ):
        await safe_answer_callback(callback, "این دکمه معتبر نیست.", show_alert=True)
        return
    action, group_id = parts[1], message.chat.id
    if action not in {"donate", "view", "100", "500", "max", "income", "top"}:
        await safe_answer_callback(callback, "این دکمه معتبر نیست.", show_alert=True)
        return
    try:
        # Callback data never selects the spending user. Check current membership.
        member = await callback.bot.get_chat_member(group_id, callback.from_user.id)
        if member.status in {"left", "kicked"} or (
            member.status == "restricted" and not member.is_member
        ):
            raise CastleError("این قابلیت فقط برای اعضای گروه است.")
        receipt = None
        leaders = None
        if action in {"100", "500", "max", "income", "top"}:
            async with get_session() as session:
                actor = await UserService.get_or_create_user(
                    session, callback.from_user
                )
                if actor.is_banned:
                    raise CastleError("دسترسی شما محدود شده است.")
                if action in {"100", "500", "max"}:
                    amount = None if action == "max" else int(action)
                    paid = await CastleService.donate(
                        session,
                        actor.id,
                        group_id,
                        amount,
                        request_key=f"castle:cb:{callback.id}",
                    )
                    receipt = f"{fa_digits(paid)} سیب به قلعه اهدا شد!"
                elif action == "income":
                    paid = await CastleService.claim_daily_income(
                        session, actor.id, group_id
                    )
                    receipt = f"{fa_digits(paid)} سیب درآمد روزانه دریافت شد!"
                else:
                    total = func.sum(CastleDonation.amount)
                    leaders = (
                        await session.execute(
                            select(User.first_name, total)
                            .join(CastleDonation, CastleDonation.user_id == User.id)
                            .where(CastleDonation.group_id == group_id)
                            .group_by(User.id, User.first_name)
                            .order_by(total.desc(), User.id)
                            .limit(5)
                        )
                    ).all()
        await safe_answer_callback(callback, receipt or "🏰", show_alert=bool(receipt))
        if action == "top":
            text = "🏆 برترین اهداکنندگان\n" + (
                "\n".join(
                    f"{fa_digits(i)}. {escape(name[:40])}: {fa_digits(amount)} 🍏"
                    for i, (name, amount) in enumerate(leaders, 1)
                )
                or "هنوز کمکی ثبت نشده است."
            )
            await message.answer(text, parse_mode="HTML")
        else:
            await show_castle(message, group_id, donation_menu=action == "donate")
    except CastleError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
    except Exception:
        logger.exception("Castle operation or display failed")
        await safe_answer_callback(
            callback,
            "عملیات یا نمایش انجام نشد؛ پیش از تکرار، موجودی و وضعیت قلعه را بررسی کنید.",
            show_alert=True,
        )
