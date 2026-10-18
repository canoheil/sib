"""Group-only Apple Castle screens. All mutations commit before Telegram delivery."""

from html import escape
from pathlib import Path
from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import CastleDonation, Group, GroupCastle, User
from sib.services.castle_service import (
    CastleService, CastleError, THRESHOLDS,
    DAILY_INCOME_PER_LEVEL, INVASION_BUFF_PER_LEVEL,
)
from sib.services.castle_art import castle_image
from sib.services.sib_claim import fa_digits
from sib.services.user import UserService
from sib.services.telegram_ui import safe_answer_callback
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.war_service import WarError, WarService

router = Router(name="castle")
GROUPS = {"group", "supergroup"}


def keyboard(group_id, donate=False, is_admin=False):
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
        if is_admin:
            buttons.append(success_button("⚔️ شروع حمله", f"castle:war_start:{group_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def format_castle_caption(level, total_donated):
    """Render the Apple Castle status in Persian with Persian numerals."""
    next_threshold = (
        THRESHOLDS[level] if level < len(THRESHOLDS) else THRESHOLDS[-1]
    )
    buff_percent = round(level * INVASION_BUFF_PER_LEVEL * 100)
    income = level * DAILY_INCOME_PER_LEVEL
    return (
        "🏰 قلعه سیبی\n\n"
        f"📊 سطح قلعه: {fa_digits(level)} از ۱۰\n\n"
        f"🍏 سیب اهدا شده: {fa_digits(total_donated)} / {fa_digits(next_threshold)}\n\n"
        f"⚔️ قدرت حمله در یورش: +{fa_digits(buff_percent)}٪\n\n"
        f"🎁 درآمد روزانه: {fa_digits(income)} سیب\n\n"
        "⚠️ سقف اهدا: روزانه ۵۰۰ سیب (برای هر بازیکن)\n\n"
        "ℹ️ اهدای سیب بازگشت‌پذیر نیست. درآمد روزانه فقط از یک قلعه دریافت می‌شود."
    )


async def show_castle(message, group_id, *, donation_menu=False, is_admin=False):
    async with get_session() as session:
        castle = await CastleService.get_or_create_castle(session, group_id)
        level, total = castle.level, castle.total_donated
    caption = format_castle_caption(level, total)
    markup = keyboard(group_id, donation_menu, is_admin=is_admin)
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
        is_admin = False
        if message.from_user:
            try:
                member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
                is_admin = member.status in ("creator", "administrator")
            except Exception:
                pass
        await show_castle(message, message.chat.id, is_admin=is_admin)
    except CastleError as exc:
        await message.answer(str(exc))


@router.callback_query(F.data.startswith("castle:"))
async def castle_callback(callback: CallbackQuery):
    message = callback.message
    parts = (callback.data or "").split(":")
    if (
        not isinstance(message, Message)
        or message.chat.type not in GROUPS
        or len(parts) not in {3, 4}
        or parts[2] != str(message.chat.id)
        or callback.from_user.is_bot
    ):
        await safe_answer_callback(callback, "این دکمه معتبر نیست.", show_alert=True)
        return
    action, group_id = parts[1], message.chat.id
    valid_actions = {
        "donate", "view", "100", "500", "max", "income", "top",
        "war_start", "war_pick", "war_confirm"
    }
    if action not in valid_actions:
        await safe_answer_callback(callback, "این دکمه معتبر نیست.", show_alert=True)
        return
    try:
        # Callback data never selects the spending user. Check current membership.
        member = await callback.bot.get_chat_member(group_id, callback.from_user.id)
        if member.status in {"left", "kicked"} or (
            member.status == "restricted" and not member.is_member
        ):
            raise CastleError("این قابلیت فقط برای اعضای گروه است.")
        is_admin = member.status in ("creator", "administrator")

        if action == "war_start":
            if not is_admin:
                await safe_answer_callback(
                    callback, "تنها مدیران گروه اجازهٔ اعلان جنگ دارند.", show_alert=True
                )
                return
            async with get_session() as session:
                targets = await WarService.find_target_groups(session, group_id)
            if not targets:
                await safe_answer_callback(
                    callback,
                    "هیچ گروه دشمن مناسبی (اختلاف سطح حداکثر ۳ یا بدون سپر صلح) در دسترس نیست.",
                    show_alert=True,
                )
                return
            buttons = [
                [
                    primary_button(
                        f"🏰 {title[:20]} (سطح {fa_digits(c.level)})",
                        f"castle:war_pick:{group_id}:{c.group_id}",
                    )
                ]
                for c, title in targets
            ]
            buttons.append([danger_button("🔙 بازگشت", f"castle:view:{group_id}")])
            text = (
                "⚔️ انتخاب هدف برای نبرد (جدال سیبی)\n\n"
                "🎯 گروه‌های زیر با اختلاف سطح حداکثر ±۳ و بدون سپر صلح در دسترس هستند:\n"
                "یکی از قلعه‌ها را برای تهاجم انتخاب کنید:"
            )
            await safe_answer_callback(callback, "🎯 انتخاب هدف")
            await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            return

        if action == "war_pick":
            if not is_admin:
                await safe_answer_callback(
                    callback, "تنها مدیران گروه اجازهٔ اعلان جنگ دارند.", show_alert=True
                )
                return
            target_id = int(parts[3])
            async with get_session() as session:
                target_group = await session.get(Group, target_id)
                target_castle = await session.get(GroupCastle, target_id)
            if not target_group or not target_castle:
                await safe_answer_callback(callback, "قلعهٔ هدف یافت نشد.", show_alert=True)
                return
            text = (
                "⚠️ تایید اعلان جنگ ۵ ساعته\n\n"
                f"🎯 قلعهٔ هدف: {target_group.title}\n"
                f"📊 سطح قلعه حریف: {fa_digits(target_castle.level)} از ۱۰\n\n"
                "⏳ مدت زمان نبرد: ۵ ساعت تمام\n"
                "💰 پاداش فتح: غارت ۱۵٪ از پیشرفت اهدای قلعهٔ حریف\n"
                "🛡 قانون صلح: گروه مغلوب ۱۲ ساعت سپر صلح دریافت می‌کند.\n\n"
                "آیا از شروع حمله به این قلعه اطمینان دارید؟"
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [success_button("🚀 شروع حمله", f"castle:war_confirm:{group_id}:{target_id}")],
                    [danger_button("🔙 انصراف", f"castle:view:{group_id}")],
                ]
            )
            await safe_answer_callback(callback, "⚠️ تایید نبرد")
            await message.answer(text, reply_markup=markup)
            return

        if action == "war_confirm":
            if not is_admin:
                await safe_answer_callback(
                    callback, "تنها مدیران گروه اجازهٔ اعلان جنگ دارند.", show_alert=True
                )
                return
            target_id = int(parts[3])
            async with get_session() as session:
                war = await WarService.declare_war(session, group_id, target_id)
                attacker_group = await session.get(Group, group_id)
                defender_group = await session.get(Group, target_id)
                attacker_title = attacker_group.title if attacker_group else f"گروه {group_id}"
                defender_title = defender_group.title if defender_group else f"گروه {target_id}"

            await safe_answer_callback(callback, "🚀 نبرد ۵ ساعته آغاز شد!", show_alert=True)

            declaration_text = (
                "🚨 طبل جنگ در اواریا به صدا درآمد! 🚨\n\n"
                f"⚔️ گروه «{attacker_title}» رسماً به قلعهٔ «{defender_title}» اعلان جنگ داد!\n"
                "⏳ مهلت نبرد: ۵ ساعت تمام\n"
                "🍏 غنیمت فتح: تاراج بخشی از پیشرفت اهدای قلعهٔ مغلوب\n\n"
                "💥 دلاوران دو قبیله! کارت‌های اصلی خود را بردارید و با ارسال دستور «وضعیت جنگ» در گروه خود، به میدان نبرد بشتابید!"
            )
            war_banner_path = Path("assets/apple_clash/war_bg_declaration.jpg")
            atk_markup = InlineKeyboardMarkup(
                inline_keyboard=[[danger_button("🔙 بازگشت", f"castle:view:{group_id}")]]
            )
            def_markup = InlineKeyboardMarkup(
                inline_keyboard=[[danger_button("🔙 بازگشت", f"castle:view:{target_id}")]]
            )

            # Deliver to attacker group (DO NOT PIN)
            try:
                if war_banner_path.is_file():
                    await callback.bot.send_photo(
                        chat_id=group_id,
                        photo=FSInputFile(str(war_banner_path)),
                        caption=declaration_text,
                        reply_markup=atk_markup,
                    )
                else:
                    await callback.bot.send_message(
                        chat_id=group_id,
                        text=declaration_text,
                        reply_markup=atk_markup,
                    )
            except Exception:
                logger.exception("Failed to send declaration to attacker group %s", group_id)

            # Deliver to defender group (DO NOT PIN)
            try:
                if war_banner_path.is_file():
                    await callback.bot.send_photo(
                        chat_id=target_id,
                        photo=FSInputFile(str(war_banner_path)),
                        caption=declaration_text,
                        reply_markup=def_markup,
                    )
                else:
                    await callback.bot.send_message(
                        chat_id=target_id,
                        text=declaration_text,
                        reply_markup=def_markup,
                    )
            except Exception:
                logger.exception("Failed to send declaration to defender group %s", target_id)
            return

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
            await show_castle(
                message, group_id, donation_menu=action == "donate", is_admin=is_admin
            )
    except (CastleError, WarError) as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
    except Exception:
        logger.exception("Castle operation or display failed")
        await safe_answer_callback(
            callback,
            "عملیات یا نمایش انجام نشد؛ پیش از تکرار، موجودی و وضعیت قلعه را بررسی کنید.",
            show_alert=True,
        )
