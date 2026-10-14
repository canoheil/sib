"""Owner-bound private inline dashboard for the Phase 6 control commands."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from sib.admin.auth import IsAdminFilter, is_admin
from sib.database.models import AdminLog, Inv2GroupState
from sib.database.connection import get_session
from sib.services.invasion_v2 import admin_controls as ops
from sib.services.invasion_v2.presentation import _telegram
from sib.services.maintenance.service import invalidate_invasion_config_cache
from .inv2_controls import _execute, _private, _run

router = Router(name="inv2_dashboard")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


def keyboard(admin_id: int) -> InlineKeyboardMarkup:
    def b(text, action):
        return InlineKeyboardButton(text=text, callback_data=f"inv2adm:{admin_id}:{action}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("روشن‌کردن", "on"), b("خاموش‌کردن", "off"), b("تازه‌سازی", "refresh")],
        [b("نواحی", "regions"), b("گروه‌های خاموش", "muted"), b("نبردهای فعال", "active")],
        [b("تنظیمات", "config"), b("سلامت", "health"), b("شبیه‌سازی", "simulate")],
    ])


async def _muted(session):
    rows = (await session.scalars(select(Inv2GroupState).where(
        Inv2GroupState.muted_until > ops.NOW()).order_by(Inv2GroupState.group_id).limit(20))).all()
    return "🔕 گروه‌های خاموش\n" + ("\n".join(
        f"{r.group_id} · تا {ops._date(r.muted_until)}" for r in rows) or "گروهی خاموش نیست.")


async def _simulate_hint(session):
    return "⚔️ برای شبیه‌سازی: /inv2_simulate شناسهٔ گروه و (اختیاری) گونهٔ دشمن"


@router.message(Command("inv2_dashboard"))
async def dashboard(message: Message):
    if not message.from_user or not is_admin(message.from_user.id) or not _private(message):
        return
    text = await _execute(message.from_user.id, "inv2_dashboard", [], ops.status, 0, 0)
    await _telegram(None, message.answer, text=text[:4000], reply_markup=keyboard(message.from_user.id), parse_mode=None)


@router.callback_query(F.data.startswith("inv2adm:"))
async def dashboard_action(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id) or not _private(callback):
        await _telegram(None, callback.answer, text="دسترسی ندارید.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdecimal() or int(parts[1]) != callback.from_user.id:
        await _telegram(None, callback.answer, text="این صفحه متعلق به مدیر دیگری است.", show_alert=True)
        return
    action = parts[2]
    admin_id = callback.from_user.id
    if action == "on":
        text = await _run(admin_id, "inv2_toggle", ["on"])
        if text.startswith("⚔️ تهاجم"):
            invalidate_invasion_config_cache()
    elif action == "off":
        text = await _run(admin_id, "inv2_toggle", ["off"])
        if text.startswith("⚔️ تهاجم"):
            invalidate_invasion_config_cache()
    elif action == "refresh":
        text = await _execute(admin_id, "inv2adm_refresh", [], ops.status, 0, 0)
    elif action == "regions":
        text = await _run(admin_id, "inv2_regions", [])
    elif action == "muted":
        text = await _execute(admin_id, "inv2adm_muted", [], _muted, 0, 0)
    elif action == "active":
        text = await _run(admin_id, "inv2_invasions", ["ACTIVE"])
    elif action == "config":
        text = await _run(admin_id, "inv2_config", [])
    elif action == "health":
        text = await _run(admin_id, "inv2_health", [])
    elif action == "simulate":
        # An inline button has no group ID to pass to the simulation.
        text = await _execute(admin_id, "inv2adm_simulate", [], _simulate_hint, 0, 0)
    else:
        await _telegram(None, callback.answer, text="دکمه معتبر نیست.", show_alert=True)
        return
    await _telegram(None, callback.answer, text="تازه شد.")
    if callback.message:
        await _telegram(None, callback.message.edit_text, text=text[:4000],
                        reply_markup=keyboard(admin_id), parse_mode=None)
