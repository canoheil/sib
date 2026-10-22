"""Apple Clash (Group War) UI handlers. Status screen, attack execution, healing potion and battle dispatches."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image
from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from sib.core.logging import logger
from sib.core.text import format_html, plain_display_name, plain_text, safe_name
from sib.database.connection import get_session
from sib.database.models import Character, Group, GroupWar, User
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.character_art import artwork_path
from sib.services.player_cards import PlayerCardService
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.user import UserService
from sib.services.war.imaging import compose_war_attack_image
from sib.services.war.narrative import (
    render_attacker_narrative,
    render_defender_narrative,
)
from sib.services.war_service import WarError, WarService

router = Router(name="war")
GROUPS = {"group", "supergroup"}


def war_keyboard(war_id: int, group_id: int, is_attacker: bool) -> InlineKeyboardMarkup:
    """Build glass-button keyboard with correct semantic colors for attacker or defender."""
    if is_attacker:
        action_buttons = [
            success_button("⚔️ حمله به قلعه", f"war:atk:{war_id}:{group_id}:normal"),
            primary_button("💥 حمله با سیب باف", f"war:atk:{war_id}:{group_id}:buff"),
        ]
    else:
        action_buttons = [
            success_button("🛡 دفاع از قلعه", f"war:atk:{war_id}:{group_id}:normal"),
            primary_button("💥 دفاع با سیب باف", f"war:atk:{war_id}:{group_id}:buff"),
        ]

    rows = [
        [action_buttons[0]],
        [action_buttons[1]],
        [primary_button("🧪 استفاده از معجون شفا", f"war:potion:{war_id}:{group_id}")],
        [danger_button("🔙 بازگشت", f"castle:view:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_war_caption(
    attacker_title: str,
    attacker_damage: int,
    defender_title: str,
    defender_damage: int,
    remaining_seconds: int,
    player_energy: int,
) -> str:
    hours, rem = divmod(max(0, remaining_seconds), 3600)
    minutes = rem // 60
    time_str = f"{fa_digits(hours)} ساعت و {fa_digits(minutes)} دقیقه"

    return (
        "⚔️ **جدال سیبی**\n"
        f"🔴 مهاجم: **{attacker_title}**؛ آسیب: **{fa_digits(attacker_damage)}**\n"
        f"🔵 مدافع: **{defender_title}**؛ آسیب: **{fa_digits(defender_damage)}**\n"
        f"⏳ زمان باقی‌مانده: **{time_str}**\n"
        f"⚡️ انرژی جنگی شما: **{fa_digits(player_energy)} از ۱۰۰**\n"
        "ℹ️ هر ضربه **۲۰ انرژی**؛ فاصله: **۳۰ ثانیه**."
    )


NO_WAR_TEXT = "⚔️ جنگ فعالی نداریم.\nمدیران می‌تونن از منوی «قلعه سیبی» اعلام جنگ کنن."


async def build_war_status_screen(group_id: int, user_id: int):
    """(caption, reply_markup, photo) for the war status menu; no delivery.

    ``None`` when the group has no active war. ``photo`` is an
    ``FSInputFile`` for the side's background, or ``None`` when the asset
    is not deployed (a text screen). Viewing the status registers the
    viewer as a war participant, exactly as before.
    """
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        war = await WarService.get_active_war(session, group_id)
        if not war:
            return None

        attacker = await session.get(Group, war.attacker_id)
        defender = await session.get(Group, war.defender_id)
        attacker_title = attacker.title if attacker else f"گروه {war.attacker_id}"
        defender_title = defender.title if defender else f"گروه {war.defender_id}"

        part = await WarService.get_or_create_participant(
            session, war.id, user_id, group_id
        )

        ends_at = (
            war.ends_at.replace(tzinfo=timezone.utc)
            if war.ends_at.tzinfo is None
            else war.ends_at
        )
        remaining_sec = int((ends_at - now).total_seconds())
        is_attacker = group_id == war.attacker_id

        caption = format_war_caption(
            attacker_title,
            war.attacker_damage,
            defender_title,
            war.defender_damage,
            remaining_sec,
            part.energy,
        )
        markup = war_keyboard(war.id, group_id, is_attacker)

    bg_name = "war_bg_attacker.jpg" if is_attacker else "war_bg_defender.jpg"
    bg_path = Path("assets/apple_clash") / bg_name
    photo = FSInputFile(str(bg_path)) if bg_path.is_file() else None
    return caption, markup, photo


async def send_war_status(message: Message, group_id: int, user_id: int):
    """Send a NEW war status menu (the «وضعیت جنگ» text entry only)."""
    screen = await build_war_status_screen(group_id, user_id)
    if screen is None:
        await message.answer(NO_WAR_TEXT)
        return
    caption, markup, photo = screen
    if photo is not None:
        try:
            await message.answer_photo(photo, caption=format_html(caption),
                                       reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            logger.warning(
                "War photo delivery failed; falling back to text", exc_info=True
            )

    await message.answer(format_html(caption), reply_markup=markup, parse_mode="HTML")


async def edit_war_status(callback: CallbackQuery, group_id: int, user_id: int) -> bool:
    """Refresh the war status on the message behind a callback (C2-7).

    Attack/defend/potion clicks update damage and energy in place instead
    of posting another status photo into the group. When the war is over
    the clicker just gets the no-war alert.
    """
    screen = await build_war_status_screen(group_id, user_id)
    if screen is None:
        await safe_answer_callback(callback, NO_WAR_TEXT, show_alert=True)
        return False
    caption, markup, photo = screen
    return await safe_edit_callback_message(
        callback, callback.bot, format_html(caption), reply_markup=markup,
        photo=photo, parse_mode="HTML",
    )


async def get_player_card_bytes(session, user_id: int) -> bytes:
    """Retrieve framed card art bytes for the user's main card, or return fallback canvas."""
    card = await PlayerCardService.get_main_card(session, user_id)
    if card:
        character = await session.get(Character, card.character_id)
        if character:
            path = artwork_path(character)
            if path and path.is_file():
                return path.read_bytes()

    # Fallback themed card canvas
    canvas = Image.new("RGBA", (390, 530), "#1A2238")
    out = BytesIO()
    canvas.convert("RGB").save(out, format="JPEG")
    return out.getvalue()


def _mention(user_id: int, first_name: Optional[str]) -> str:
    """Format a clickable mention link for teammates."""
    return f"[{safe_name(first_name, 'یک سیبی')}](tg://user?id={int(user_id)})"


async def _send_dispatch(bot, chat_id: int, photo_bytes: bytes, caption: str):
    """Deliver battle dispatch photo + caption, with graceful fallback to text.

    The dispatch carries the ``**`` narrative, so the primary send is HTML.
    Every degraded retry drops to *plain* text (no parse mode) rather than
    replaying HTML with the parse mode switched off, which would show the
    ``<b>`` tags to both groups.
    """
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(photo_bytes, filename="attack.jpg"),
            caption=format_html(caption),
            parse_mode="HTML",
        )
        return
    except Exception:
        logger.warning("War dispatch HTML send failed chat=%s; retrying plain",
                       chat_id, exc_info=True)
    plain = plain_text(caption)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(photo_bytes, filename="attack.jpg"),
            caption=plain,
            parse_mode=None,
        )
        return
    except Exception:
        pass
    try:
        await bot.send_message(chat_id=chat_id, text=plain, parse_mode=None)
    except Exception:
        logger.exception("Failed to deliver war dispatch to chat %s", chat_id)


@router.message(F.chat.type.in_(GROUPS), F.text == "وضعیت جنگ")
async def war_status_entry(message: Message):
    if not message.from_user:
        return
    await send_war_status(message, message.chat.id, message.from_user.id)


@router.callback_query(F.data.startswith("war:atk:"))
async def war_attack_callback(callback: CallbackQuery):
    message = callback.message
    if not isinstance(message, Message) or not callback.from_user:
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await safe_answer_callback(callback, "درخواست نامعتبر است.", show_alert=True)
        return

    war_id = int(parts[2])
    group_id = int(parts[3])
    mode = parts[4]  # "normal" or "buff"
    user_id = callback.from_user.id

    try:
        member = await callback.bot.get_chat_member(group_id, user_id)
        if member.status in {"left", "kicked"} or (
            member.status == "restricted" and not member.is_member
        ):
            await safe_answer_callback(
                callback, "این قابلیت فقط برای اعضای گروه است.", show_alert=True
            )
            return

        async with get_session() as session:
            actor = await UserService.get_or_create_user(session, callback.from_user)
            if actor.is_banned:
                await safe_answer_callback(
                    callback, "دسترسی شما محدود شده است.", show_alert=True
                )
                return

            damage, energy, card_name = await WarService.attack(
                session, war_id, user_id, group_id, is_buff_used=(mode == "buff")
            )
            # C2-8.5: persist the spent energy, the ++attack_count counter,
            # last_attack_at and the war damage BEFORE any image/render work.
            # The attack is durable even if a later UI step fails, and the
            # next click reads the committed cooldown/energy/counter.
            await session.commit()

            war = await session.get(GroupWar, war_id)
            if not war:
                raise WarError("اطلاعات نبرد یافت نشد.")

            attacker_id = war.attacker_id
            defender_id = war.defender_id

            attacker_group = await session.get(Group, attacker_id)
            defender_group = await session.get(Group, defender_id)
            attacker_title = (
                attacker_group.title if attacker_group else f"گروه {attacker_id}"
            )
            defender_title = (
                defender_group.title if defender_group else f"گروه {defender_id}"
            )

            card_bytes = await get_player_card_bytes(session, user_id)

        # Notify clicking user
        alert_msg = (
            f"💥 سیب باف! {fa_digits(damage)} آسیب.\nانرژی باقی‌مانده: {fa_digits(energy)}"
            if mode == "buff"
            else f"⚔️ {fa_digits(damage)} آسیب ثبت شد!\nانرژی باقی‌مانده: {fa_digits(energy)}"
        )
        await safe_answer_callback(callback, alert_msg, show_alert=True)

        # Determine perspectives
        is_actor_attacker = group_id == attacker_id
        perspective = "attacker" if is_actor_attacker else "defender"
        target_title = defender_title if is_actor_attacker else attacker_title
        source_title = attacker_title if is_actor_attacker else defender_title

        # Compose attack battle card
        img_bytes = compose_war_attack_image(
            war_id=war_id,
            user_id=user_id,
            card_image_bytes=card_bytes,
            damage=damage,
            is_buff=(mode == "buff"),
            perspective=perspective,
        )

        # Attacker group gets clickable teammate mention
        # Defender group gets plain sanitized name (no links, no IDs)
        attacker_mention = _mention(user_id, callback.from_user.first_name)
        defender_plain_player = plain_display_name(
            callback.from_user.first_name, fallback="مهاجم"
        )

        text_attacker = render_attacker_narrative(
            player=attacker_mention,
            card=card_name,
            target_group=target_title,
            damage=damage,
            is_buff=(mode == "buff"),
        )
        text_defender = render_defender_narrative(
            player=defender_plain_player,
            card=card_name,
            source_group=source_title,
            damage=damage,
            is_buff=(mode == "buff"),
        )

        # Send battle dispatches to BOTH groups:
        # The actor's group gets attacker perspective with teammate link,
        # The enemy group gets defender perspective without links/IDs.
        actor_chat_id = group_id
        enemy_chat_id = defender_id if is_actor_attacker else attacker_id
        await _send_dispatch(callback.bot, actor_chat_id, img_bytes, text_attacker)
        await _send_dispatch(callback.bot, enemy_chat_id, img_bytes, text_defender)

        # C2-7: the dispatches above are cross-group announcements and stay
        # new messages; the status menu behind this click instead refreshes
        # in place (new damage totals + the clicker's remaining energy).
        await edit_war_status(callback, group_id, user_id)

    except WarError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
    except Exception:
        logger.exception("War attack failed")
        await safe_answer_callback(
            callback, "ثبت ضربه انجام نشد؛ لطفا دوباره تلاش کنید.", show_alert=True
        )


@router.callback_query(F.data.startswith("war:potion:"))
async def war_potion_callback(callback: CallbackQuery):
    message = callback.message
    if not isinstance(message, Message) or not callback.from_user:
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await safe_answer_callback(callback, "درخواست نامعتبر است.", show_alert=True)
        return

    war_id = int(parts[2])
    group_id = int(parts[3])
    user_id = callback.from_user.id

    try:
        member = await callback.bot.get_chat_member(group_id, user_id)
        if member.status in {"left", "kicked"} or (
            member.status == "restricted" and not member.is_member
        ):
            await safe_answer_callback(
                callback, "این قابلیت فقط برای اعضای گروه است.", show_alert=True
            )
            return

        async with get_session() as session:
            actor = await UserService.get_or_create_user(session, callback.from_user)
            if actor.is_banned:
                await safe_answer_callback(
                    callback, "دسترسی شما محدود شده است.", show_alert=True
                )
                return

            new_energy = await WarService.use_healing_potion(
                session, war_id, user_id, group_id
            )
            # C2-8.5: the inventory decrement and the capped energy are
            # committed together; a later UI failure cannot resurrect the
            # potion or roll back the heal.
            await session.commit()

        await safe_answer_callback(
            callback,
            f"🧪 معجون شفا: ۵۰ انرژی برگشت!\nانرژی فعلی: {fa_digits(new_energy)}",
            show_alert=True,
        )

        # C2-7: show the restored energy on the status menu in place.
        await edit_war_status(callback, group_id, user_id)

    except WarError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
    except Exception:
        logger.exception("War healing potion failed")
        await safe_answer_callback(
            callback, "مصرف معجون با خطا مواجه شد.", show_alert=True
        )
