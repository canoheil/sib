"""Admin control center: user resources + channel management (Phase 7).

Data-driven flows built on the EXISTING authoritative systems:
- apples  -> AppleWalletService ledger (earn / spend, fully audited)
- saplings -> SeedlingService.admin_adjust (clamped at 0)
- channels -> SponsorService (upsert / toggle / remove) + ChannelType

Dynamic numeric input (user id, amounts) goes through aiogram FSM states;
no fixed amount shortcuts, no parallel balance stores.
"""

import re
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.admin.auth import IsAdminFilter
from sib.core.logging import Events, log_event
from sib.database.connection import get_session
from sib.database.models import ChannelType, User
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.seedling import SeedlingService
from sib.services.sponsor.service import SponsorService

router = Router(name="admin_center")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

MAX_ADJUST = 1_000_000

RESOURCES = ("apples", "saplings")
RESOURCE_LABEL = {"apples": "🍎 سیب", "saplings": "🌱 نهال"}


class AdminUserFlow(StatesGroup):
    waiting_user = State()
    waiting_amount = State()


class AdminChannelFlow(StatesGroup):
    waiting_channel = State()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _back_admin_main() -> list:
    return [InlineKeyboardButton(text="🔙 بازگشت به منوی ادمین", callback_data="admin_main")]


async def _resource_line(session, user_id: int) -> str:
    apples = await AppleWalletService.get_balance(session, user_id)
    saplings = await SeedlingService.get_total(session, user_id)
    return f"🍎 سیب: `{apples}` | 🌱 نهال: `{saplings}`"


async def _user_panel(session, user_id: int) -> tuple:
    """(text, kb) of the user resource control panel (DB authoritative)."""
    user = await session.get(User, user_id)
    if user is None:
        return None, None
    name = user.first_name or "بدون‌نام"
    uname = f" (@{user.username})" if user.username else ""
    text = (
        "👤 **مدیریت منابع کاربر** 🍏\n\n"
        f"کاربر: {name}{uname} — `{user.id}`\n"
        f"{await _resource_line(session, user.id)}\n\n"
        "عملیات و مبلغ را انتخاب کن؛ مبلغ را به‌صورت عدد می‌فرستی:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🍎 + افزودن سیب",
                    callback_data=f"admin_user_op:{user.id}:apples:add",
                ),
                InlineKeyboardButton(
                    text="🍎 − کاهش سیب",
                    callback_data=f"admin_user_op:{user.id}:apples:sub",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌱 + افزودن نهال",
                    callback_data=f"admin_user_op:{user.id}:saplings:add",
                ),
                InlineKeyboardButton(
                    text="🌱 − کاهش نهال",
                    callback_data=f"admin_user_op:{user.id}:saplings:sub",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی",
                    callback_data=f"admin_user:{user.id}",
                )
            ],
            _back_admin_main(),
        ]
    )
    return text, kb


async def _send_user_panel(message: Message, user_id: int) -> None:
    async with get_session() as session:
        text, kb = await _user_panel(session, user_id)
    if text is None:
        await message.reply("🍏 کاربر پیدا نشد.")
        return
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# User resource management
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "admin_users")
async def handle_admin_users(callback: CallbackQuery, state: FSMContext):
    # Entry points ALWAYS work (they restart the flow), regardless of any
    # previously stuck FSM state.
    await state.set_state(AdminUserFlow.waiting_user)
    await callback.answer()
    text = (
        "👤 **مدیریت کاربران** 🍏\n\n"
        "شناسه عددی کاربر را بفرست — یا روی یکی از پیام‌هایش ریپلای کن:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[_back_admin_main()])
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.message(AdminUserFlow.waiting_user, F.text)
async def handle_admin_user_input(message: Message, state: FSMContext):
    user_id: Optional[int] = None
    reply = message.reply_to_message
    reply_user = getattr(reply, "from_user", None) if reply else None
    if reply_user and not getattr(reply_user, "is_bot", False):
        # Reply-to-target resolves to a HUMAN user only; a reply to the bot
        # must never resolve to the bot's own id.
        user_id = reply_user.id
    else:
        m = re.search(r"-?\d+", message.text or "")
        if m:
            user_id = abs(int(m.group()))

    if not user_id:
        # Invalid input: retry in-step (state kept).
        await message.reply("🍏 یک شناسه عددی معتبر بفرست (یا روی پیام کاربر ریپلای کن).")
        return

    async with get_session() as session:
        exists = await session.get(User, user_id) is not None
    if not exists:
        # Not found: EXIT the flow (state cleared) — never leak into a loop
        # where every next message is treated as a user id.
        await state.clear()
        await message.reply(f"🍏 کاربری با شناسه `{user_id}` پیدا نشد.", parse_mode="Markdown")
        return

    await state.set_state(AdminUserFlow.waiting_amount)
    await state.update_data(target_user_id=user_id, pending_op=None)
    await _send_user_panel(message, user_id)


@router.callback_query(F.data.startswith("admin_user_op:"))
async def handle_admin_user_op(callback: CallbackQuery, state: FSMContext):
    _head, user_id_s, resource, sign = callback.data.split(":")
    if resource not in RESOURCES or sign not in ("add", "sub"):
        await callback.answer("عملیات نامعتبر 🍏", show_alert=True)
        return
    await state.set_state(AdminUserFlow.waiting_amount)
    await state.update_data(
        target_user_id=int(user_id_s), pending_op={"resource": resource, "sign": sign}
    )
    label = RESOURCE_LABEL[resource]
    verb = "افزودن" if sign == "add" else "کاهش"
    await callback.answer()
    text = f"💳 **{verb} {label}**\n\nمبلغ را به‌صورت عدد بفرست (مثلاً `500`):"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ انصراف", callback_data=f"admin_user:{user_id_s}")]]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.message(AdminUserFlow.waiting_amount, F.text)
async def handle_admin_amount_input(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    op = data.get("pending_op")
    user_id = data.get("target_user_id")
    if not op or not user_id:
        # No operation is pending (panel view / expired prompt). NEVER fall
        # back to the user-id step — that leaked every message into an id.
        await state.clear()
        await message.reply("🍏 عملیات فعالی نیست؛ از دکمه‌های پنل ادمین شروع کن.")
        return

    m = re.fullmatch(r"\s*(\d+)\s*", message.text or "")
    if not m or not (1 <= int(m.group(1)) <= MAX_ADJUST):
        await message.reply(f"🍏 مبلغ نامعتبر؛ یک عدد بین `1` تا `{MAX_ADJUST}` بفرست.", parse_mode="Markdown")
        return
    amount = int(m.group(1))

    resource, sign = op["resource"], op["sign"]
    delta = amount if sign == "add" else -amount
    admin_id = message.from_user.id if message.from_user else None

    try:
        async with get_session() as session:
            if resource == "apples":
                if delta > 0:
                    await AppleWalletService.earn(
                        session, user_id, delta, reason="admin_adjust",
                        ref_type="admin", ref_id=admin_id,
                    )
                else:
                    await AppleWalletService.spend(
                        session, user_id, -delta, reason="admin_adjust",
                        ref_type="admin", ref_id=admin_id,
                    )
            else:
                await SeedlingService.admin_adjust(
                    session, user_id, delta, reason="admin_adjust"
                )
            line = await _resource_line(session, user_id)
            text_panel, kb = await _user_panel(session, user_id)
    except InsufficientFundsException:
        # Invalid amount for this op: retry in-step (state kept).
        await message.reply("🍏 سیب‌های این کاربر برای این کاهش کافی نیست؛ مبلغ دیگری بفرست.")
        return

    log_event(
        Events.ADMIN_COMMAND_EXECUTED,
        {"user_id": admin_id, "command": f"admin_adjust {resource} {delta:+d} -> {user_id}"},
    )
    label = RESOURCE_LABEL[resource]
    verb = "اضافه شد" if delta > 0 else "کم شد"
    # Success: EXIT the flow so no later message is ever captured as input.
    await state.clear()
    await message.reply(
        f"✅ `{abs(delta)}` {label} از نوع {verb}.\nوضعیت جدید: {line}",
        parse_mode="Markdown",
    )
    if text_panel:
        await message.reply(text_panel, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin_user:"))
async def handle_admin_user_refresh(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        text, kb = await _user_panel(session, user_id)
    await callback.answer()
    if text is None:
        await callback.answer("کاربر پیدا نشد 🍏", show_alert=True)
        return
    await state.set_state(AdminUserFlow.waiting_amount)
    await state.update_data(target_user_id=user_id, pending_op=None)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Channel management
# ---------------------------------------------------------------------------


def _channel_row(ch) -> InlineKeyboardButton:
    status_icon = "🟢" if ch.is_active else "🔴"
    label = f"{status_icon} {ch.title or ch.channel_id} — {ChannelType.label(ch.channel_type)}"
    return InlineKeyboardButton(text=label, callback_data=f"admin_ch:{ch.channel_id}")


async def _channels_screen(session) -> tuple:
    channels = await SponsorService.get_all_channels(session)
    text = (
        "🤝 **کانال‌های رسمی و اسپانسر (گیت اسپانسر)** 🍏\n\n"
        "کانال را برای مدیریت انتخاب کن (فعال/غیرفعال، نوع، اجباری، حذف):\n"
        f"تعداد کل: `{len(channels)}`"
    )
    buttons = [[_channel_row(ch)] for ch in channels]
    buttons.append(
        [InlineKeyboardButton(text="➕ افزودن کانال", callback_data="admin_ch_add")]
    )
    buttons.append(_back_admin_main())
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_channels")
async def handle_admin_channels(callback: CallbackQuery):
    async with get_session() as session:
        text, kb = await _channels_screen(session)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


async def _channel_detail_view(ch_id: str) -> tuple:
    """(text, kb) of one channel's control screen (DB authoritative)."""
    async with get_session() as session:
        ch = await SponsorService.get_channel(session, ch_id)
    if ch is None:
        return None, None

    status_text = "🟢 فعال" if ch.is_active else "🔴 غیرفعال"
    req_text = "🔒 اجباری" if ch.is_required else "🔓 اختیاری"
    text = (
        f"🤝 **کانال:** {ch.title or ch.channel_id}\n\n"
        f"🆔 شناسه: `{ch.channel_id}`\n"
        f"📌 نوع: {ChannelType.label(ch.channel_type)}\n"
        f"⚙️ وضعیت: {status_text} | {req_text}\n"
        f"⭐ اولویت: `{ch.priority}`\n"
        f"🔗 لینک: {ch.invite_link or '—'}\n"
        f"🕓 ایجاد: {ch.created_at or '—'}"
    )
    toggle_label = "🔴 غیرفعال‌سازی" if ch.is_active else "🟢 فعال‌سازی"
    req_label = "🔓 اختیاری کردن" if ch.is_required else "🔒 اجباری کردن"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data=f"admin_ch_toggle:{ch.channel_id}")],
            [InlineKeyboardButton(text=req_label, callback_data=f"admin_ch_req:{ch.channel_id}")],
            [InlineKeyboardButton(text="✏️ تغییر نوع کانال", callback_data=f"admin_ch_type:{ch.channel_id}")],
            [InlineKeyboardButton(text="🗑 حذف کانال", callback_data=f"admin_ch_del:{ch.channel_id}")],
            [InlineKeyboardButton(text="🔙 بازگشت به کانال‌ها", callback_data="admin_channels")],
        ]
    )
    return text, kb


async def _render_channel_detail(message: Message, ch_id: str) -> None:
    text, kb = await _channel_detail_view(ch_id)
    if text is None:
        return
    await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin_ch:"))
async def handle_admin_channel_detail(callback: CallbackQuery):
    ch_id = callback.data.split(":", 1)[1]
    text, _kb = await _channel_detail_view(ch_id)
    if text is None:
        await callback.answer("کانال پیدا نشد 🍏", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _render_channel_detail(callback.message, ch_id)


@router.callback_query(F.data.startswith("admin_ch_toggle:"))
async def handle_admin_channel_toggle(callback: CallbackQuery):
    """Enable/disable (existing semantics preserved: never a delete)."""
    ch_id = callback.data.split(":", 1)[1]
    async with get_session() as session:
        ch = await SponsorService.toggle_active(session, ch_id)
        await session.commit()
    if ch:
        state_str = "فعال" if ch.is_active else "غیرفعال"
        await callback.answer(f"وضعیت کانال {ch.title} به {state_str} تغییر یافت! 🍏")
    else:
        await callback.answer("کانال پیدا نشد 🍏", show_alert=True)
        return
    if callback.message:
        await _render_channel_detail(callback.message, ch_id)


@router.callback_query(F.data.startswith("admin_ch_req:"))
async def handle_admin_channel_required(callback: CallbackQuery):
    ch_id = callback.data.split(":", 1)[1]
    async with get_session() as session:
        ch = await SponsorService.toggle_required(session, ch_id)
        await session.commit()
    if ch:
        req_str = "اجباری" if ch.is_required else "اختیاری"
        await callback.answer(f"کانال {ch.title} اکنون {req_str} است 🍏")
    else:
        await callback.answer("کانال پیدا نشد 🍏", show_alert=True)
        return
    if callback.message:
        await _render_channel_detail(callback.message, ch_id)


@router.callback_query(F.data.startswith("admin_ch_type:"))
async def handle_admin_channel_type_picker(callback: CallbackQuery):
    ch_id = callback.data.split(":", 1)[1]
    buttons = [
        [
            InlineKeyboardButton(
                text=ChannelType.label(t),
                callback_data=f"admin_ch_set_type:{ch_id}:{t}",
            )
        ]
        for t in ChannelType.ALL
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"admin_ch:{ch_id}")])
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "📌 نوع کانال را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data.startswith("admin_ch_set_type:"))
async def handle_admin_channel_set_type(callback: CallbackQuery):
    _head, ch_id, new_type = callback.data.split(":")
    async with get_session() as session:
        ch = await SponsorService.get_channel(session, ch_id)
        if ch is not None:
            ch.channel_type = new_type
            await session.commit()
    if ch is None:
        await callback.answer("کانال پیدا نشد 🍏", show_alert=True)
        return
    log_event(
        Events.ADMIN_COMMAND_EXECUTED,
        {"user_id": callback.from_user.id if callback.from_user else None,
         "command": f"channel_type {ch_id} -> {new_type}"},
    )
    await callback.answer(f"نوع کانال به {ChannelType.label(new_type)} تغییر کرد 🍏")
    if callback.message:
        await _render_channel_detail(callback.message, ch_id)


@router.callback_query(F.data.startswith("admin_ch_del:"))
async def handle_admin_channel_delete_confirm(callback: CallbackQuery):
    ch_id = callback.data.split(":", 1)[1]
    text = (
        "🗑 **حذف کانال**\n\n"
        "این عملیات رکورد کانال را برای همیشه حذف می‌کند.\n"
        "برای غیرفعال‌سازی موقت از دکمه‌ی فعال/غیرفعال استفاده کن.\n\n"
        "آیا مطمئنی؟"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 بله، حذف کن", callback_data=f"admin_ch_del_yes:{ch_id}")],
            [InlineKeyboardButton(text="↩️ انصراف", callback_data=f"admin_ch:{ch_id}")],
        ]
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin_ch_del_yes:"))
async def handle_admin_channel_delete(callback: CallbackQuery):
    ch_id = callback.data.split(":", 1)[1]
    async with get_session() as session:
        removed = await SponsorService.remove_channel(session, ch_id)
        await session.commit()
        text, kb = await _channels_screen(session)
    log_event(
        Events.ADMIN_COMMAND_EXECUTED,
        {"user_id": callback.from_user.id if callback.from_user else None,
         "command": f"channel_delete {ch_id}"},
    )
    await callback.answer("کانال حذف شد 🍏" if removed else "کانال پیدا نشد 🍏", show_alert=not removed)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


def _parse_channel_ref(text: str):
    """@username / -100id / t.me link -> value usable with bot.get_chat."""
    t = (text or "").strip()
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)", t)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", t):
        return t if t.startswith("@") else "@" + t
    m = re.fullmatch(r"-?\d+", t)
    if m:
        return int(t)
    return None


@router.callback_query(F.data == "admin_ch_add")
async def handle_admin_channel_add_start(callback: CallbackQuery, state: FSMContext):
    # Entry points ALWAYS work (they restart the flow), regardless of any
    # previously stuck FSM state. This was the root cause of "Add Channel
    # does nothing": the button was silently filtered out whenever a stale
    # state was still active.
    await state.set_state(AdminChannelFlow.waiting_channel)
    await callback.answer()
    text = (
        "➕ **افزودن کانال** 🍏\n\n"
        "لینک یا شناسه‌ی کانال را بفرست:\n"
        "مثال: `@mychannel` یا `https://t.me/mychannel` یا `-1001234567890`"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به کانال‌ها", callback_data="admin_channels")]]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.message(AdminChannelFlow.waiting_channel, F.text)
async def handle_admin_channel_add_input(message: Message, state: FSMContext, bot: Bot):
    ref = _parse_channel_ref(message.text or "")
    if ref is None:
        # Invalid input: retry in-step (state kept).
        await message.reply("🍏 لینک یا شناسه معتبر نیست؛ دوباره بفرست.")
        return
    try:
        chat = await bot.get_chat(ref)
    except Exception:
        # Not found: EXIT the flow (state cleared).
        await state.clear()
        await message.reply(
            "🍏 کانال پیدا نشد یا ربات عضو آن نیست.\n"
            "ابتدا ربات را ادمین کانال کن و از «افزودن کانال» دوباره شروع کن."
        )
        return

    title = chat.title or chat.full_name or str(chat.id)
    username = getattr(chat, "username", None)
    invite_link = f"https://t.me/{username}" if username else ""
    await state.update_data(
        pending_channel={
            "channel_id": str(chat.id),
            "title": title,
            "invite_link": invite_link,
        }
    )
    buttons = [
        [
            InlineKeyboardButton(
                text=ChannelType.label(t),
                callback_data=f"admin_ch_add_type:{t}",
            )
        ]
        for t in ChannelType.ALL
    ]
    await message.reply(
        f"✅ کانال پیدا شد: **{title}**\n\n📌 نوع کانال را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )


@router.callback_query(AdminChannelFlow.waiting_channel, F.data.startswith("admin_ch_add_type:"))
async def handle_admin_channel_add_finish(callback: CallbackQuery, state: FSMContext):
    new_type = callback.data.split(":", 1)[1]
    if new_type not in ChannelType.ALL:
        await callback.answer("نوع نامعتبر 🍏", show_alert=True)
        return
    data = await state.get_data()
    pending = data.get("pending_channel")
    if not pending:
        await callback.answer("جریان منقضی شده؛ دوباره شروع کن 🍏", show_alert=True)
        await state.set_state(default_state)
        return

    async with get_session() as session:
        ch = await SponsorService.add_or_update_channel(
            session,
            channel_id=pending["channel_id"],
            title=pending["title"],
            invite_link=pending["invite_link"],
            channel_type=new_type,
            is_required=True,
            is_active=True,
        )
        await session.commit()
        text, kb = await _channels_screen(session)

    log_event(
        Events.ADMIN_COMMAND_EXECUTED,
        {"user_id": callback.from_user.id if callback.from_user else None,
         "command": f"channel_add {ch.channel_id} type={new_type}"},
    )
    await state.set_state(default_state)
    await callback.answer(f"کانال {ch.title} با موفقیت اضافه شد! 🍏")
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
