"""Expanded user dashboard; pending edits are bound to admin, target and panel."""
import logging
import secrets
from dataclasses import dataclass, field as dcfield
from time import monotonic
from aiogram import Router, F
from aiogram.filters import Command
from sqlalchemy import select
from sib.admin.auth import is_admin
from sib.core.bidi import fix_bidi, wrap_ltr
from sib.database.connection import get_session
from sib.database.models import Character
from sib.keyboards.common import keyboard_from_specs
from sib.services.admin_user_actions import FIELDS, snapshot, mutate
from sib.services.sib_claim import fa_digits

router = Router(name="admin_users_expanded")
logger = logging.getLogger(__name__)
_lookup = {}
# Required composite key: (admin_id, user_id, field, mode).
_pending_amounts = {}

@dataclass
class Edit:
    chat_id: int
    panel_id: int
    value: object = None
    token: str = dcfield(default_factory=lambda: secrets.token_hex(8))
    step: int = 0
    touched: float = dcfield(default_factory=monotonic)

def kb(rows):
    """Build a panel keyboard from ``(text, callback_data[, style])`` specs.

    Styles: success = confirm/add, danger = cancel/remove/back/reset,
    primary = the rest of this dashboard's navigation. A spec without a style
    gets blue (primary): this panel never renders a transparent button.
    """
    return keyboard_from_specs(rows, default_style="primary")

def clear(uid):
    for key in list(_pending_amounts):
        if key[0] == uid or monotonic() - _pending_amounts[key].touched > 600:
            del _pending_amounts[key]

async def authorized(event):
    if event.from_user and is_admin(event.from_user.id):
        return True
    await event.answer("⛔ فقط مدیران اجازه دسترسی دارند.")
    return False

async def dashboard(session, uid):
    user, values, cards = await snapshot(session, uid)
    text = fix_bidi(
        f"👤 کاربر: {wrap_ltr(user.display_name)} ({uid})\n"
        f"نام کاربری: @{wrap_ltr(user.username or 'ندارد')}\n"
        f"تاریخ ثبت: {user.created_at}\n"
    )
    text += "\n".join(f"{label}: {values[key]}" for key,label in FIELDS.items())
    text += f"\n🃏 کارت‌ها: {len(cards)}\n🚫 وضعیت: {'مسدود' if user.is_banned else 'فعال'}"
    text += "\nتعداد گروه‌ها: نامشخص"
    rows = [[("🍏 سیب‌ها", "apples"), ("🃏 کارت‌ها", "cards")],
            [("⭐ سطح و تجربه", "level"), ("🚫 مسدودیت", "ban")],
            [("⏱ ریست کول‌داون‌ها", "cooldowns"), ("🌱 نهال", "seedling")]]
    buttons = [[(label, f"uadm:menu:{menu}:{uid}", "primary") for label,menu in row] for row in rows]
    buttons.append([("🔄 بروزرسانی", f"uadm:refresh:{uid}", "primary"),
                    ("❌ بستن", "uadm:close", "danger")])
    return fa_digits(text), kb(buttons)

async def render(bot, chat_id, panel_id, uid):
    async with get_session() as session:
        text, keyboard = await dashboard(session, uid)
    await bot.edit_message_text(chat_id=chat_id, message_id=panel_id, text=text,
                                reply_markup=keyboard, parse_mode=None)

@router.message(Command("users"))
async def command(message):
    if not await authorized(message):
        return
    clear(message.from_user.id)
    from sib.handlers.admin_broadcast import _pending_broadcast
    _pending_broadcast.pop(message.from_user.id, None)
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        try:
            async with get_session() as session:
                text, keyboard = await dashboard(session, reply.from_user.id)
            await message.answer(text, reply_markup=keyboard, parse_mode=None)
        except Exception:
            await message.answer("کاربر پیدا نشد یا پنل در دسترس نیست.")
    else:
        _lookup[message.from_user.id] = (message.chat.id, monotonic())
        await message.answer("شناسه عددی کاربر را بفرستید یا روی پیام او پاسخ دهید.")

@router.callback_query(F.data == "admin_users")
async def entry(callback, state):
    if not await authorized(callback):
        return
    await state.clear()
    from sib.handlers.admin_broadcast import _pending_broadcast
    _pending_broadcast.pop(callback.from_user.id, None)
    clear(callback.from_user.id)
    _lookup[callback.from_user.id] = (callback.message.chat.id, monotonic())
    await callback.answer()
    await callback.message.answer("شناسه عددی کاربر را بفرستید یا روی پیام او پاسخ دهید.")


@router.callback_query(F.data.startswith("admin_user_op:") | F.data.startswith("admin_user:"))
async def legacy(callback, state):
    if not await authorized(callback):
        return
    await state.clear()
    await callback.answer("پنل قدیمی منقضی شده است؛ مدیریت کاربران را دوباره باز کنید.", show_alert=True)

@router.callback_query(F.data.startswith("uadm:"))
async def control(callback, bot):
    if not await authorized(callback):
        return
    aid = callback.from_user.id
    try:
        parts = callback.data.split(":")
        action = parts[1]
        if action == "close":
            clear(aid)
            _lookup.pop(aid, None)
            await callback.message.edit_text("پنل بسته شد.", reply_markup=None)
            await callback.answer()
            return
        if action == "confirm":
            token = parts[2]
            match = next(((key, edit) for key,edit in _pending_amounts.items()
                          if key[0] == aid and edit.token == token), None)
            if not match:
                raise ValueError("درخواست منقضی شده است.")
            key, edit = match
            _, uid, field, mode = key
            if monotonic() - edit.touched > 600 or edit.chat_id != callback.message.chat.id or edit.value is None:
                clear(aid)
                raise ValueError("درخواست منقضی شده است.")
            if is_admin(uid) and uid != aid and edit.step == 0:
                edit.step = 1
                edit.touched = monotonic()
                await callback.message.edit_text("⚠️ این حساب مدیر است. تأیید دوم لازم است؛ ادامه می‌دهید؟",
                    reply_markup=kb([[("✅ تأیید دوم", callback.data, "success")],
                                     [("❌ لغو", f"uadm:back:{uid}", "danger")]]))
                await callback.answer()
                return
            _pending_amounts.pop(key, None)
            async with get_session() as session:
                note = await mutate(session, aid, uid, field, mode, edit.value,
                                    confirmed=True, admin_confirmed=edit.step == 1)
                await session.commit()
            await callback.answer("✅ تغییر ثبت شد.")
            await render(bot, edit.chat_id, edit.panel_id, uid)
            await callback.message.edit_text("✅ تغییر ثبت شد." + ("\n" + note if note else ""), reply_markup=None)
            return
        uid = int(parts[-1])
        if uid <= 0:
            raise ValueError("شناسه نامعتبر است.")
        if action in ("open", "back", "refresh"):
            clear(aid)
            await render(bot, callback.message.chat.id, callback.message.message_id, uid)
        elif action == "menu":
            clear(aid)
            menu = parts[2]
            rows = []
            text = "گزینه موردنظر را انتخاب کنید:"
            if menu in ("apples", "level", "seedling"):
                fields = ["apples", "apple_gold", "apple_diamond", "apple_obsidian", "magic_powder"] if menu == "apples" else ["level", "xp"] if menu == "level" else ["seedling"]
                for field in fields:
                    rows.append([(f"➕ {FIELDS[field]}", f"uadm:add:{field}:{uid}", "success"),
                                 (f"➖ {FIELDS[field]}", f"uadm:sub:{field}:{uid}", "danger")])
            elif menu == "cards":
                async with get_session() as session:
                    _, _, cards = await snapshot(session, uid)
                    lines = []
                    for card in cards:
                        character = await session.get(Character, card.character_id)
                        lines.append(f"{character.code if character else 'نامشخص'} {'⭐' if card.is_favorite else ''}")
                text = "🃏 کارت‌ها:\n" + ("\n".join(lines) or "بدون کارت")
                rows = [[("➕ افزودن", f"uadm:add:card:{uid}", "success"),
                         ("➖ حذف", f"uadm:sub:card:{uid}", "danger")],
                        [("تنظیم کارت اصلی", f"uadm:set:card:{uid}", "primary")]]
            elif menu == "ban":
                async with get_session() as session:
                    user, _, _ = await snapshot(session, uid)
                rows = [[("رفع مسدودیت" if user.is_banned else "مسدودسازی",
                          f"uadm:{'unban' if user.is_banned else 'ban'}:{uid}",
                          "success" if user.is_banned else "danger")]]
            elif menu == "cooldowns":
                text = "کول‌داون‌ها از تاریخچه پاداش محاسبه می‌شوند؛ ریست ایمن فعلاً در دسترس نیست."
                rows = [[("ریست معدن", f"uadm:reset:mine:{uid}", "danger"),
                         ("ریست دریافت روزانه", f"uadm:reset:claim:{uid}", "danger")]]
            else:
                raise ValueError("گزینه نامعتبر است.")
            rows.append([("🔙 بازگشت", f"uadm:back:{uid}", "danger")])
            await callback.message.edit_text(fa_digits(text)[:4000], reply_markup=kb(rows), parse_mode=None)
        elif action in ("add", "sub", "set", "ban", "unban", "reset"):
            clear(aid)
            field = "ban" if action in ("ban", "unban") else parts[2]
            mode = "set" if action in ("ban", "unban", "reset") else action
            if field not in FIELDS and field not in ("ban", "mine", "claim", "card"):
                raise ValueError("فیلد نامعتبر است.")
            edit = Edit(callback.message.chat.id, callback.message.message_id)
            _pending_amounts[(aid, uid, field, mode)] = edit
            if field in ("ban", "mine", "claim"):
                edit.value = action == "ban"
                await confirmation(callback.message, uid, field, edit)
            else:
                await callback.message.answer("کد کارت را بفرستید." if field == "card" else "مقدار مثبت را وارد کنید.")
        else:
            raise ValueError("گزینه نامعتبر است.")
        await callback.answer()
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
    except Exception:
        logger.exception("User admin callback failed")
        await callback.answer("خطایی رخ داد؛ پنل را دوباره باز کنید.", show_alert=True)

async def confirmation(message, uid, field, edit):
    display_value = ("بله" if edit.value else "خیر") if isinstance(edit.value, bool) else edit.value
    text = f"⚠️ تغییر {FIELDS.get(field, 'وضعیت کاربر')} برای کاربر {uid} به {display_value} را تأیید می‌کنید؟"
    if field == "ban":
        text = f"🚫 {'مسدودسازی' if edit.value else 'رفع مسدودیت'} کاربر {uid} را تأیید می‌کنید؟\nهیچ داده‌ای حذف نمی‌شود. بررسی پارتی به تعویق می‌افتد."
    await message.answer(fa_digits(text), reply_markup=kb([[("✅ تأیید", f"uadm:confirm:{edit.token}", "success")],
                                                        [("❌ لغو", f"uadm:back:{uid}", "danger")]]), parse_mode=None)

@router.message(lambda m: bool(m.from_user and not (m.text or "").startswith("/") and (m.from_user.id in _lookup or any(k[0] == m.from_user.id for k in _pending_amounts))))
async def receive(message):
    if not await authorized(message):
        return
    aid = message.from_user.id
    try:
        lookup = _lookup.get(aid)
        if lookup:
            if monotonic() - lookup[1] > 600:
                _lookup.pop(aid, None)
                raise ValueError("درخواست منقضی شده است.")
            if lookup[0] != message.chat.id:
                return
            reply = message.reply_to_message
            uid = reply.from_user.id if reply and reply.from_user and not reply.from_user.is_bot else int(message.text or "")
            async with get_session() as session:
                text, keyboard = await dashboard(session, uid)
            _lookup.pop(aid, None)
            await message.answer(text, reply_markup=keyboard, parse_mode=None)
            return
        key, edit = next((k,e) for k,e in _pending_amounts.items() if k[0] == aid)
        if edit.chat_id != message.chat.id:
            return
        if monotonic() - edit.touched > 600:
            clear(aid)
            raise ValueError("درخواست منقضی شده است.")
        if edit.value is not None:
            await message.answer("ابتدا درخواست قبلی را تأیید یا لغو کنید.")
            return
        value = (message.text or "").strip()
        if key[2] != "card":
            value = int(value)
            if not 0 < value <= 2_000_000_000:
                raise ValueError("مقدار مثبت معتبر وارد کنید.")
        edit.value = value
        edit.touched = monotonic()
        await confirmation(message, key[1], key[2], edit)
    except ValueError:
        await message.answer("شناسه، مقدار یا کد معتبر وارد کنید؛ ممکن است درخواست منقضی شده باشد.")
    except Exception:
        logger.exception("User admin input failed")
        await message.answer("خطایی رخ داد؛ دوباره پنل را باز کنید.")
