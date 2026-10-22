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
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.war_service import (
    PEACE_SHIELD_DURATION,
    WAR_DURATION,
    WarError,
    WarService,
    shield_remaining_seconds,
)

router = Router(name="castle")
GROUPS = {"group", "supergroup"}

# C2-6: shown before a shielded group may browse war targets. Confirming it
# breaks the shield at once; cancelling keeps the castle protected.
WAR_SHIELD_WARNING = (
    "⚠️ **سپر صلح فعاله!**\n"
    "حمله کنی، سپر می‌شکنه و قلعه‌ات بدون محافظ می‌مونه."
)

WAR_TARGET_PROMPT = (
    "⚔️ **هدف جنگ رو انتخاب کن**\n"
    "🎯 اختلاف سطح تا **۳**؛ فقط قلعه‌های **بدون سپر**."
)

WAR_NO_TARGETS = (
    "هدف مناسبی پیدا نشد؛ اختلاف سطح باید تا ۳ باشه و هدف سپر نداشته باشه."
)


def _duration_words(duration) -> tuple[str, str]:
    """(«یک ساعت», «یک\u200cساعته») for one hour; («۲ ساعت», «۲ ساعته») otherwise."""
    hours = int(duration.total_seconds() // 3600)
    if hours == 1:
        return "یک ساعت", "یک\u200cساعته"
    return f"{fa_digits(hours)} ساعت", f"{fa_digits(hours)} ساعته"


# C2-9: these texts are built from the constants so a duration change can
# never leave the announcement promising the old number.
WAR_HOURS_TEXT, WAR_HOURS_ADJECTIVE = _duration_words(WAR_DURATION)
SHIELD_HOURS_TEXT = fa_digits(int(PEACE_SHIELD_DURATION.total_seconds() // 3600))


def format_remaining_time(seconds: int) -> str:
    """Persian duration for the shield line (e.g. «۴ ساعت و ۱۲ دقیقه»)."""
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes = rest // 60
    if hours == 0 and minutes == 0:
        return "کمتر از یک دقیقه"
    return f"{fa_digits(hours)} ساعت و {fa_digits(minutes)} دقیقه"


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


def format_castle_caption(level, total_donated, shield_seconds=None):
    """Render the Apple Castle status in Persian with Persian numerals.

    ``shield_seconds`` is the remaining lifetime of an ACTIVE peace shield
    (C2-6); a shield line is only added when there is one. Expired or absent
    shields leave the caption exactly as it was.
    """
    next_threshold = (
        THRESHOLDS[level] if level < len(THRESHOLDS) else THRESHOLDS[-1]
    )
    buff_percent = round(level * INVASION_BUFF_PER_LEVEL * 100)
    income = level * DAILY_INCOME_PER_LEVEL
    shield_line = (
        f"🛡 سپر صلح: **{format_remaining_time(shield_seconds)} دیگر**\n"
        if shield_seconds is not None
        else ""
    )
    return (
        "🏰 **قلعه سیبی**\n"
        f"📊 سطح: **{fa_digits(level)} از ۱۰**\n"
        f"{shield_line}"
        f"🍏 اهدا: **{fa_digits(total_donated)} از {fa_digits(next_threshold)}**\n"
        f"⚔️ قدرت یورش: **+{fa_digits(buff_percent)}٪**\n"
        f"🎁 درآمد روزانه: **{fa_digits(income)} سیب**\n"
        "⚠️ سقف اهدا: **۵۰۰ سیب در روز** (هر بازیکن)\n"
        "ℹ️ اهدا برنمی‌گرده؛ درآمد روزانه فقط از **یک قلعه**."
    )


async def build_castle_screen(group_id, *, donation_menu=False, is_admin=False):
    """(caption, reply_markup, photo) for the castle menu; no delivery.

    ``photo`` is an ``FSInputFile`` for the level art, or ``None`` when no
    art is deployed (a text screen). Both delivery paths below render this
    one screen, so the sent and edited menus can never drift apart.
    """
    async with get_session() as session:
        castle = await CastleService.get_or_create_castle(session, group_id)
        level, total = castle.level, castle.total_donated
        shield_seconds = shield_remaining_seconds(castle.shield_until)
    caption = format_castle_caption(level, total, shield_seconds)
    markup = keyboard(group_id, donation_menu, is_admin=is_admin)
    image = castle_image(level)
    return caption, markup, FSInputFile(image) if image else None


async def send_castle(message, group_id, *, donation_menu=False, is_admin=False):
    """Send a NEW castle menu (the «قلعه سیبی» text entry only)."""
    caption, markup, photo = await build_castle_screen(
        group_id, donation_menu=donation_menu, is_admin=is_admin
    )
    if photo is not None:
        try:
            await message.answer_photo(photo, caption=caption, reply_markup=markup)
            return
        except Exception:
            logger.warning("Castle photo delivery failed; using text", exc_info=True)
    await message.answer(caption, reply_markup=markup)


async def edit_castle(callback, group_id, *, donation_menu=False, is_admin=False):
    """Render the castle menu onto the message behind a callback (C2-7).

    Callback flows must never post a fresh menu into the group: the shared
    transition engine edits the photo/caption in place, and an unchanged
    screen is a harmless no-op instead of another message.
    """
    caption, markup, photo = await build_castle_screen(
        group_id, donation_menu=donation_menu, is_admin=is_admin
    )
    return await safe_edit_callback_message(
        callback, callback.bot, caption, reply_markup=markup, photo=photo
    )


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
        await send_castle(message, message.chat.id, is_admin=is_admin)
    except CastleError as exc:
        await message.answer(str(exc))


async def offer_war_targets(callback: CallbackQuery, group_id: int) -> bool:
    """Show the eligible enemy castles; True when the list is on screen.

    C2-6: only castles WITHOUT an active peace shield are listed (the service
    query filters them out), so a protected group can never be picked.
    C2-7: the list edits the menu message in place, never a new message.
    """
    async with get_session() as session:
        targets = await WarService.find_target_groups(session, group_id)
    if not targets:
        await safe_answer_callback(callback, WAR_NO_TARGETS, show_alert=True)
        return False
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
    await safe_answer_callback(callback, "🎯 انتخاب هدف")
    return await safe_edit_callback_message(
        callback,
        callback.bot,
        WAR_TARGET_PROMPT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


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
        "war_start", "war_pick", "war_confirm", "war_break_shield_confirm",
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
            # C2-6: a shielded group is warned first - attacking breaks the
            # shield immediately. The target list only opens after confirmation.
            async with get_session() as session:
                shield_until = await WarService.active_shield(session, group_id)
            if shield_until is not None:
                warning_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            success_button(
                                "✅ بشکن و ادامه بده",
                                f"castle:war_break_shield_confirm:{group_id}",
                            )
                        ],
                        [danger_button("🔙 انصراف", f"castle:view:{group_id}")],
                    ]
                )
                await safe_answer_callback(callback, "🛡 سپر صلح فعال است", show_alert=True)
                await safe_edit_callback_message(
                    callback, callback.bot, WAR_SHIELD_WARNING, reply_markup=warning_markup
                )
                return
            await offer_war_targets(callback, group_id)
            return

        if action == "war_break_shield_confirm":
            if not is_admin:
                await safe_answer_callback(
                    callback, "تنها مدیران گروه اجازهٔ اعلان جنگ دارند.", show_alert=True
                )
                return
            async with get_session() as session:
                broken = await WarService.break_shield(session, group_id)
            if broken:
                await safe_answer_callback(callback, "🛡 سپر صلح شکسته شد")
            else:
                await safe_answer_callback(callback, "🛡 سپر فعالی وجود ندارد")
            await offer_war_targets(callback, group_id)
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
                f"⚠️ **اعلام جنگ {WAR_HOURS_ADJECTIVE}**\n"
                f"🎯 هدف: **{target_group.title}**؛ سطح: **{fa_digits(target_castle.level)} از ۱۰**\n"
                f"⏳ نبرد: **{WAR_HOURS_TEXT}**\n"
                "🍏 غنیمت: بخشی از پیشرفت قلعهٔ دشمن؛ مقدار دقیق بعد از پیروزی مشخص میشه.\n"
                f"🛡 بعد از نبرد، مدافع **{SHIELD_HOURS_TEXT} ساعت سپر** می‌گیره؛ مهاجم **سپر نداره**.\n"
                "**حمله شروع بشه؟**"
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [success_button("🚀 شروع حمله", f"castle:war_confirm:{group_id}:{target_id}")],
                    [danger_button("🔙 انصراف", f"castle:view:{group_id}")],
                ]
            )
            await safe_answer_callback(callback, "⚠️ تایید نبرد")
            await safe_edit_callback_message(
                callback, callback.bot, text, reply_markup=markup
            )
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

            await safe_answer_callback(
                callback, f"🚀 نبرد {WAR_HOURS_ADJECTIVE} آغاز شد!", show_alert=True
            )

            declaration_text = (
                "🚨 **جنگ شروع شد!**\n"
                f"⚔️ **{attacker_title}** به **{defender_title}** اعلام جنگ کرد.\n"
                f"⏳ مهلت: **{WAR_HOURS_TEXT}**\n"
                "🍏 غنیمت پیروزی: بخشی از پیشرفت قلعهٔ بازنده.\n"
                "«وضعیت جنگ» رو بفرست و وارد نبرد شو."
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
                    receipt = f"🍏 {fa_digits(paid)} سیب به قلعه رفت!"
                elif action == "income":
                    paid = await CastleService.claim_daily_income(
                        session, actor.id, group_id
                    )
                    receipt = f"🎁 {fa_digits(paid)} سیب درآمد روزانه گرفتی!"
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
            await safe_edit_callback_message(
                callback,
                callback.bot,
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [danger_button("🔙 بازگشت", f"castle:view:{group_id}")]
                    ]
                ),
                parse_mode="HTML",
            )
        else:
            await edit_castle(
                callback, group_id, donation_menu=action == "donate", is_admin=is_admin
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
