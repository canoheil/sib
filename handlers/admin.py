"""Admin panel handlers."""

from datetime import datetime, timedelta, timezone
import inspect
from typing import Optional

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import select, func, update
from sqlalchemy.dialects import mysql, sqlite
from sib.admin.auth import IsAdminFilter, is_admin
from sib.core.logging import log_event, Events
from sib.core.text import format_html
from sib.database.connection import get_session
from sib.database.models import (
    AdminLog, User, Group, Game, Match, AdCampaign, SponsorChannel, Inv2GroupMember,
    SystemConfig,
)
from sib.keyboards.common import get_admin_main_keyboard, danger_button, primary_button
from sib.services.ad.service import AdService
from sib.services import disease_service
from sib.services.halloween import (
    END_ANNOUNCEMENT,
    END_TEST_CONFIRMATION,
    START_ANNOUNCEMENT,
    START_TEST_CONFIRMATION,
    HalloweenService,
    invalidate_halloween_config_cache,
)
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
async def handle_admin_main_menu(callback: CallbackQuery, state: FSMContext = None):
    """Return to admin main menu. Always EXITS any admin FSM flow."""
    if state is not None:
        await state.clear()
    from sib.handlers.admin_users import clear, _lookup
    from sib.handlers.admin_broadcast import _pending_broadcast
    clear(callback.from_user.id)
    _lookup.pop(callback.from_user.id, None)
    _pending_broadcast.pop(callback.from_user.id, None)
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
            [primary_button(text="🔄 بروزرسانی", callback_data="admin_dashboard")],
            [danger_button(text="🔙 بازگشت به منوی ادمین", callback_data="admin_main")],
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
        buttons.append([primary_button(text=label, callback_data=f"admin_ad_view:{c.id}")])

    buttons.append([danger_button(text="🔙 بازگشت به منوی اصلی", callback_data="admin_main")])
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
                [primary_button(text=toggle_btn_text, callback_data=f"admin_ad_toggle:{campaign.id}")],
                [danger_button(text="🔙 بازگشت به لیست تبلیغات", callback_data="admin_ads")],
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
                primary_button(
                    text="🔄 بروزرسانی",
                    callback_data="admin_matches",
                )
            ],
            [
                danger_button(
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


# ---------------------------------------------------------------------------
# Halloween Global Event Admin Controls (/hw_start, /hw_end, /hw_status)
# ---------------------------------------------------------------------------


async def _reply_admin_message(message: Message, text: str) -> None:
    answer_fn = getattr(message, "answer", None)
    reply_fn = getattr(message, "reply", None)
    if callable(answer_fn) and inspect.iscoroutinefunction(answer_fn):
        await answer_fn(text)
    elif callable(reply_fn) and inspect.iscoroutinefunction(reply_fn):
        await reply_fn(text)


async def _broadcast_to_active_groups(
    bot: Optional[Bot],
    message: Message,
    group_ids: tuple[int, ...],
    text: str,
) -> int:
    from sib.services.maintenance.service import MaintenanceService

    target_bot = bot or getattr(message, "bot", None) or MaintenanceService._bot
    if target_bot is None:
        return 0
    sent_count = 0
    for gid in group_ids:
        try:
            await target_bot.send_message(
                gid, format_html(text), parse_mode="HTML"
            )
            sent_count += 1
        except Exception:
            continue
    return sent_count


DISEASE_START_ANNOUNCEMENT = (
    "🦠 **پایان سیبومورتیس، آغاز یک کابوس جدید!**\n\n"
    "با نابودی سیبومورتیس، انرژی شیطانی در سراسر اواریا پخش شده است. بیماری مرموزی در حال شیوع است!\n\n"
    "وضعیت خود را با دستور «سیب من» بررسی کنید و هرچه سریع‌تر درمان شوید."
)


_DISEASE_WAVE_FLAG = "disease_initial_wave_triggered"
_DISEASE_WAVE_IN_PROGRESS = '"in_progress"'


@router.message(Command("disease_start"))
async def handle_disease_start(message: Message, bot: Optional[Bot] = None) -> None:
    """Seed and announce the initial disease wave at most once."""
    if not message.from_user or not is_admin(message.from_user.id):
        return

    async with get_session() as session:
        # The upsert serializes even a missing flag across processes. Only a
        # literal false can be claimed; true and in-progress both fail closed.
        values = {"key": _DISEASE_WAVE_FLAG, "value": "false"}
        dialect = session.get_bind().dialect.name
        if dialect in ("mysql", "mariadb"):
            seed = mysql.insert(SystemConfig).values(**values)
            seed = seed.on_duplicate_key_update(key=SystemConfig.key)
        elif dialect == "sqlite":
            seed = sqlite.insert(SystemConfig).values(**values).on_conflict_do_nothing(
                index_elements=["key"]
            )
        else:
            raise RuntimeError(f"Unsupported disease wave flag dialect: {dialect}")
        await session.execute(seed)
        claim = await session.execute(
            update(SystemConfig)
            .where(SystemConfig.key == _DISEASE_WAVE_FLAG, SystemConfig.value == "false")
            .values(value=_DISEASE_WAVE_IN_PROGRESS)
        )
        claimed = claim.rowcount == 1
        if claimed:
            now = datetime.now(timezone.utc)
            # Match the six-hour tick: the available activity tracker observes
            # groups, so private-only interactions are not included.
            active_user_ids = list((await session.scalars(
                select(User.id)
                .join(Inv2GroupMember, Inv2GroupMember.user_id == User.id)
                .join(Group, Group.id == Inv2GroupMember.group_id)
                .where(
                    Inv2GroupMember.last_seen_at >= now - timedelta(hours=24),
                    Inv2GroupMember.last_seen_at <= now,
                    User.is_active.is_(True), User.is_bot.is_(False),
                    User.is_banned.is_(False), Group.is_active.is_(True),
                )
                .distinct().order_by(User.id)
            )).all())
            count = await disease_service.process_initial_infection(session, active_user_ids)
            group_ids = tuple((await session.scalars(
                select(Group.id).where(Group.is_active.is_(True)).order_by(Group.id)
            )).all())

    if not claimed:
        await message.reply("⚠️ موج اولیه بیماری قبلاً اجرا شده است.")
        return

    # The claim and infections are committed before Telegram I/O. If the
    # process stops mid-broadcast, the claim remains and prevents a repeat.
    try:
        await _broadcast_to_active_groups(bot, message, group_ids, DISEASE_START_ANNOUNCEMENT)
    finally:
        async with get_session() as session:
            finalized = await session.execute(
                update(SystemConfig)
                .where(
                    SystemConfig.key == _DISEASE_WAVE_FLAG,
                    SystemConfig.value == _DISEASE_WAVE_IN_PROGRESS,
                )
                .values(value="true")
            )
            if finalized.rowcount != 1:
                raise RuntimeError("Disease initial wave flag changed during broadcast")

    await message.reply(f"✅ موج اولیه بیماری با موفقیت شروع شد. تعداد مبتلایان: {count}")


@router.message(Command("hw_start"))
async def handle_hw_start(message: Message, bot: Optional[Bot] = None) -> None:
    """Start a new 7-day Halloween event with ``<max_hp>`` (optional ``--test`` silent mode)."""
    if not message.from_user or not is_admin(message.from_user.id):
        return

    raw_args = (message.text or "").split()[1:]
    test_mode = "--test" in raw_args
    positional = [arg for arg in raw_args if arg != "--test"]

    if not positional:
        await _reply_admin_message(
            message,
            "⚠️ لطفاً میزان جان سیبومورتیس را وارد کنید. نمونه: /hw_start 500000",
        )
        return

    raw_hp = positional[0].replace(",", "").replace("_", "")
    if not raw_hp.isdigit() or int(raw_hp) <= 0:
        await _reply_admin_message(
            message,
            "⚠️ مقدار جان نامعتبر است؛ یک عدد صحیح مثبت وارد کنید.",
        )
        return

    max_hp = int(raw_hp)
    now = datetime.now(timezone.utc)
    audit_details = f"{max_hp} --test" if test_mode else str(max_hp)

    async with get_session() as session:
        session.add(
            AdminLog(
                admin_id=message.from_user.id,
                action="hw_start",
                details=audit_details,
            )
        )
        state, group_ids = await HalloweenService.start_event(
            session, max_hp, silent=test_mode, now=now
        )
        event_id = state.id

    invalidate_halloween_config_cache()

    if test_mode:
        await _reply_admin_message(message, START_TEST_CONFIRMATION)
        return

    announcement = START_ANNOUNCEMENT
    notified = await _broadcast_to_active_groups(
        bot, message, group_ids, announcement
    )
    await _reply_admin_message(
        message,
        (
            f"🎃 ایونت هالووین (شناسه #{event_id}) با موفقیت فعال شد!\n"
            f"💀 جون سیبومورتیس: {max_hp:,} / {max_hp:,}\n"
            f"📢 پیام آغاز به {notified} گروه فعال ارسال شد."
        ),
    )


@router.message(Command("hw_end"))
async def handle_hw_end(message: Message, bot: Optional[Bot] = None) -> None:
    """Finalize Top-3 damage rewards, end the Halloween event, and broadcast unless ``--test``."""
    if not message.from_user or not is_admin(message.from_user.id):
        return

    raw_args = (message.text or "").split()[1:]
    test_mode = "--test" in raw_args
    audit_details = "manual_end --test" if test_mode else "manual_end"

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        session.add(
            AdminLog(
                admin_id=message.from_user.id,
                action="hw_end",
                details=audit_details,
            )
        )
        outcome = await HalloweenService.end_event(session, now=now)

    invalidate_halloween_config_cache()

    if test_mode:
        await _reply_admin_message(message, END_TEST_CONFIRMATION)
        return

    notified = await _broadcast_to_active_groups(
        bot, message, outcome.group_ids, END_ANNOUNCEMENT
    )
    lines = [
        "🎃 ایونت هالووین با موفقیت پایان یافت و پرچم رویداد غیرفعال شد.",
        f"📢 پیام پایان به {notified} گروه فعال ارسال شد.",
    ]
    if outcome.payouts:
        lines.append("🏆 جوایز برترین آسیب‌زنندگان واریز شد:")
        for row in outcome.payouts:
            lines.append(
                f"#{row.rank} {row.display_name} — {row.damage:,} آسیب (+{row.apples:,} سیب)"
            )
    await _reply_admin_message(message, "\n".join(lines))


@router.message(Command("hw_status"))
async def handle_hw_status(message: Message) -> None:
    """Display live Halloween boss HP, remaining time, and active attackers."""
    if not message.from_user or not is_admin(message.from_user.id):
        return

    from sib.handlers.halloween import _format_time_remaining

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        summary = await HalloweenService.get_event_status_summary(session)
        if summary.state is None:
            status_text = (
                f"🎃 وضعیت ایونت هالووین: {'فعال 🟢' if summary.enabled else 'غیرفعال 🔴'}\n"
                "هنوز هیچ دورهٔ فعالی آغاز نشده است.\n"
                "⚔️ تعداد مهاجمان فعال: 0"
            )
        else:
            st = summary.state
            rem_fa = _format_time_remaining(st.ends_at, now)
            status_text = (
                f"🎃 وضعیت ایونت هالووین: {'فعال 🟢' if (summary.enabled and st.is_active) else 'غیرفعال 🔴'}\n"
                f"💀 جون فعلی سیبومورتیس: {st.boss_hp:,} / {st.max_hp:,}\n"
                f"⏳ زمان باقی‌مانده: {rem_fa}\n"
                f"⚔️ تعداد مهاجمان فعال: {summary.active_attackers}\n"
                f"🗡 مجموع حملات ثبت‌شده: {summary.total_attacks}\n"
                f"🏆 تسویه جوایز ۳ نفر برتر: {'انجام شده ✅' if st.leaderboard_finalized else 'انجام نشده ⏳'}"
            )

    await _reply_admin_message(message, status_text)

