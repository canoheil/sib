"""Admin panel handlers."""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, func
from sib.admin.auth import IsAdminFilter
from sib.core.logging import log_event, Events
from sib.database.connection import get_session
from sib.database.models import User, Group, Game, Match, AdCampaign, SponsorChannel
from sib.keyboards.common import get_admin_main_keyboard
from sib.services.ad.service import AdService
from sib.services.sponsor.service import SponsorService

router = Router(name="admin")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.message(Command("admin"))
async def handle_admin_command(message: Message):
    """Handle /admin command."""
    user = message.from_user
    if user:
        log_event(Events.ADMIN_COMMAND_EXECUTED, {"user_id": user.id, "command": "/admin"})

    admin_text = (
        "🍏 **پنل مدیریت ربات سیب (Admin Panel)**\n\n"
        "به بخش مدیریت خوش آمدید. لطفاً گزینه مورد نظر خود را انتخاب نمایید:"
    )
    await message.reply(admin_text, reply_markup=get_admin_main_keyboard(), parse_mode="Markdown")


@router.callback_query(F.data == "admin_main")
async def handle_admin_main_menu(callback: CallbackQuery):
    """Return to admin main menu."""
    admin_text = (
        "🍏 **پنل مدیریت ربات سیب (Admin Panel)**\n\n"
        "به بخش مدیریت خوش آمدید. لطفاً گزینه مورد نظر خود را انتخاب نمایید:"
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(admin_text, reply_markup=get_admin_main_keyboard(), parse_mode="Markdown")


@router.callback_query(F.data == "admin_dashboard")
async def handle_admin_dashboard(callback: CallbackQuery):
    """Display system statistics in admin panel."""
    async with get_session() as session:
        users_count = await session.scalar(select(func.count(User.id)))
        groups_count = await session.scalar(select(func.count(Group.id)))
        games_count = await session.scalar(select(func.count(Game.id)))
        matches_count = await session.scalar(select(func.count(Match.id)))
        ad_stats = await AdService.get_analytics(session)

    dashboard_text = (
        "📊 **داشبورد وضعیت سیب**\n\n"
        f"👥 تعداد کل کاربران: `{users_count}`\n"
        f"💬 تعداد کل گروه‌ها: `{groups_count}`\n"
        f"🎮 بازی‌های فعال: `{games_count}`\n"
        f"⚔️ تعداد کل مسابقات: `{matches_count}`\n\n"
        f"📢 کمپین‌های فعال تبلیغاتی: `{ad_stats['active_campaigns']}`\n"
        f"👁️ کل نمایش تبلیغات: `{ad_stats['total_impressions']}`\n"
        f"🖱️ کل کلیک‌ها: `{ad_stats['total_clicks']}` (CTR: `{ad_stats['overall_ctr']}%`)\n\n"
        "وضعیت سرور: 🟢 آنلاین و پایدار"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="admin_dashboard")],
            [InlineKeyboardButton(text="🔙 بازگشت به منوی ادمین", callback_data="admin_main")],
        ]
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(dashboard_text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "admin_ads")
async def handle_admin_ads(callback: CallbackQuery):
    """Monetization and Ad Campaigns dashboard."""
    async with get_session() as session:
        analytics = await AdService.get_analytics(session)
        campaigns = [c for c in analytics["campaigns"] if c.is_active]

    text = (
        "📢 **مدیریت تبلیغات و مانیتایزیشن (Monetization)** 🍏\n\n"
        f"📊 کل کمپین‌ها: `{analytics['total_campaigns']}` | فعال: `{analytics['active_campaigns']}`\n"
        f"👁️ مجموع ایمپرشن: `{analytics['total_impressions']}`\n"
        f"🖱️ مجموع کلیک‌ها: `{analytics['total_clicks']}`\n"
        f"📈 میانگین CTR کل: `{analytics['overall_ctr']}%`\n\n"
        "برای مشاهده یا فعال/غیرفعال‌سازی روی هر کمپین بزنید:"
    )

    buttons = []
    for c in campaigns:
        status_icon = "🟢" if (c.is_active and not c.is_expired and not c.is_capped) else "🔴"
        label = f"{status_icon} {c.sponsor_name} — {c.campaign_name} ({c.impressions_count} نمایش)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin_ad_view:{c.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="admin_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin_ad_view:"))
async def handle_admin_ad_view(callback: CallbackQuery):
    """View details of a single ad campaign."""
    campaign_id = int(callback.data.split(":")[1])

    async with get_session() as session:
        campaign = await AdService.get_campaign(session, campaign_id)
        if not campaign:
            await callback.answer("کمپین یافت نشد 🍏", show_alert=True)
            return

        status_text = "🟢 فعال" if campaign.is_active else "🔴 غیرفعال"
        if campaign.is_expired:
            status_text += " (منقضی شده)"
        if campaign.is_capped:
            status_text += " (سقف نمایش پر شده)"

        limit_text = str(campaign.max_impressions) if campaign.max_impressions else "نامحدود"

        details_text = (
            f"📢 **کمپین:** {campaign.campaign_name}\n"
            f"🏢 **اسپانسر:** {campaign.sponsor_name}\n\n"
            f"📌 **وضعیت:** {status_text}\n"
            f"⭐ **اولویت نمایش:** `{campaign.priority}`\n"
            f"⏱️ **تناوب نمایش:** `{campaign.frequency_mode}`\n\n"
            f"👁️ **نمایش‌ها:** `{campaign.impressions_count}` / `{limit_text}`\n"
            f"🖱️ **کلیک‌ها:** `{campaign.clicks_count}`\n"
            f"📈 **نرخ کلیک (CTR):** `{campaign.ctr}%`\n\n"
            f"📝 **متن تبلیغ:**\n_{campaign.ad_text}_\n\n"
            f"🔗 **لینک هدف:** `{campaign.target_url or 'ندارد'}`"
        )

        toggle_btn_text = "🔴 غیرفعال‌سازی" if campaign.is_active else "🟢 فعال‌سازی"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=toggle_btn_text, callback_data=f"admin_ad_toggle:{campaign.id}")],
                [InlineKeyboardButton(text="🔙 بازگشت به لیست تبلیغات", callback_data="admin_ads")],
            ]
        )

        await callback.answer()
        if callback.message:
            await callback.message.edit_text(details_text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin_ad_toggle:"))
async def handle_admin_ad_toggle(callback: CallbackQuery):
    """Toggle campaign active/inactive status."""
    campaign_id = int(callback.data.split(":")[1])

    async with get_session() as session:
        campaign = await AdService.toggle_campaign_active(session, campaign_id)
        await session.commit()

        if campaign:
            state_str = "فعال" if campaign.is_active else "غیرفعال"
            await callback.answer(f"وضعیت کمپین به {state_str} تغییر یافت! 🍏")
        else:
            await callback.answer("کمپین یافت نشد 🍏", show_alert=True)

    # Refresh view
    await handle_admin_ad_view(callback)


@router.callback_query(F.data == "admin_matches")
async def handle_admin_matches(callback: CallbackQuery):
    """Display match monitoring dashboard."""
    async with get_session() as session:
        total = await session.scalar(select(func.count(Match.id)))
        active = await session.scalar(
            select(func.count(Match.id)).where(Match.state == "ACTIVE")
        )
        waiting = await session.scalar(
            select(func.count(Match.id)).where(
                Match.state == "WAITING_FOR_ACCEPTANCE"
            )
        )
        finished = await session.scalar(
            select(func.count(Match.id)).where(Match.state == "FINISHED")
        )
        cancelled = await session.scalar(
            select(func.count(Match.id)).where(Match.state == "CANCELLED")
        )

    text = (
        "⚔️ **وضعیت مسابقات**\n\n"
        f"📊 کل مسابقات: `{total or 0}`\n"
        f"🟢 فعال: `{active or 0}`\n"
        f"🟡 در انتظار پذیرش: `{waiting or 0}`\n"
        f"🏆 تمام‌شده: `{finished or 0}`\n"
        f"🔴 لغوشده: `{cancelled or 0}`"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی",
                    callback_data="admin_matches",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به منوی اصلی",
                    callback_data="admin_main",
                )
            ],
        ]
    )

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="Markdown",
        )
