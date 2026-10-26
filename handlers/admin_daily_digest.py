"""Admin controls for the once-a-day official-channel league digest."""

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from sib.admin.auth import IsAdminFilter
from sib.database.connection import get_session
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.config_store import ConfigService
from sib.services.daily_channel_digest import (
    DailyChannelDigestService,
    preview_reason_fa,
)
from sib.services.group_league import jalali_label
from sib.services.sib_claim import fa_digits

router = Router(name="admin_daily_digest")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(timezone.utc)
    config, record = await DailyChannelDigestService.control_state(now=now)
    # Share the scheduler's timezone without a second time-zone setting.
    from sib.services.daily_channel_digest import TEHRAN

    local = now.astimezone(TEHRAN)
    state = "🟢 فعال" if config.enabled else "⏸ خاموش"
    lines = [
        "🗞 مدیریت پست روزانهٔ کانال",
        "",
        f"وضعیت: {state}",
        f"مقصد: {config.channel_id or 'تنظیم نشده'}",
        f"زمان: هر روز ساعت {fa_digits(f'{config.hour:02d}:00')} به وقت تهران",
        "قانون انتشار: حداکثر یک پست در روز؛ روزِ بی‌فعالیت، پست خالی نداریم.",
    ]

    if record is None:
        if config.enabled and local.hour < config.hour:
            today_state = "هنوز زمان انتشار امروز نرسیده."
        elif config.enabled:
            today_state = "هنوز برای امروز تصمیمی ثبت نشده؛ نوبت بعدی چرخه آن را بررسی می‌کند."
        else:
            today_state = "انتشار خودکار خاموش است."
    elif record.status == "sent":
        today_state = "✅ پست امروز ارسال شده."
    elif record.status == "skipped":
        today_state = f"⏭ امروز رد شد: {preview_reason_fa(record.skip_reason or '')}"
    elif record.status == "failed":
        detail = (record.error or "خطای نامشخص").replace("\n", " ")[:180]
        today_state = f"⚠️ ارسال امروز ناموفق بود: {detail}"
        today_state += (
            "\nپیش از تلاش دوباره، کانال را بررسی کن؛ خطای شبکه گاهی پس از ثبت پیام رخ می‌دهد."
        )
    else:
        today_state = (
            "⚠️ وضعیت ارسال امروز نامشخص است. برای جلوگیری از پست تکراری، "
            "تلاش خودکار انجام نمی‌شود؛ اول کانال را بررسی کن."
        )
    lines.extend(["", f"وضعیت امروز ({jalali_label(local.date())}):", today_state])

    toggle = (
        danger_button("⏸ خاموش‌کردن انتشار", "admin_daily_digest:toggle")
        if config.enabled
        else success_button("🟢 روشن‌کردن انتشار", "admin_daily_digest:toggle")
    )
    rows = [
        [toggle],
        [primary_button("👁 پیش‌نمایش محتوای امروز", "admin_daily_digest:preview")],
    ]
    if record is not None and record.status == "failed":
        rows.append(
            [
                success_button(
                    "🔎 بررسی کانال و درخواست تلاش دوباره",
                    "admin_daily_digest:retry_confirm",
                )
            ]
        )
    rows.append([danger_button("🔙 بازگشت به پنل مدیریت", "admin_main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(message: Message) -> None:
    text, keyboard = await _panel()
    await message.answer(text, reply_markup=keyboard, parse_mode=None)


@router.message(Command("daily_digest"))
async def cmd_daily_digest(message: Message) -> None:
    if message.chat.type != "private":
        await message.reply("🗞 مدیریت پست روزانه فقط در گفت‌وگوی خصوصی مدیر در دسترس است.")
        return
    await _show(message)


@router.callback_query(F.data == "admin_daily_digest")
async def open_daily_digest(callback: CallbackQuery, state: FSMContext | None = None) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("این تنظیم فقط در گفت‌وگوی خصوصی در دسترس است.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    text, keyboard = await _panel()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)


@router.callback_query(F.data == "admin_daily_digest:toggle")
async def toggle_daily_digest(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("این تنظیم فقط در گفت‌وگوی خصوصی در دسترس است.", show_alert=True)
        return
    async with get_session() as session:
        current = await DailyChannelDigestService.load_settings(session)
        await ConfigService.set(session, "daily_digest_enabled", not current.enabled)
        await session.commit()
    await callback.answer(
        "انتشار روزانه روشن شد." if not current.enabled else "انتشار روزانه خاموش شد."
    )
    text, keyboard = await _panel()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)


@router.callback_query(F.data == "admin_daily_digest:preview")
async def preview_daily_digest(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("پیش‌نمایش فقط در گفت‌وگوی خصوصی در دسترس است.", show_alert=True)
        return
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        preview = await DailyChannelDigestService.build_preview(session, now=now)
    await callback.answer()
    if preview.ready:
        text = f"👁 پیش‌نمایش است؛ این پیام به کانال ارسال نشده:\n\n{preview.text}"
    else:
        text = f"👁 پیش‌نمایش آماده نیست:\n{preview_reason_fa(preview.reason)}"
    await callback.message.answer(text, parse_mode=None)


@router.callback_query(F.data == "admin_daily_digest:retry_confirm")
async def confirm_retry_daily_digest(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("تلاش دوباره فقط در گفت‌وگوی خصوصی در دسترس است.", show_alert=True)
        return
    await callback.answer()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
                    "کانال را بررسی کردم؛ ارسال کن",
                    "admin_daily_digest:retry",
                )
            ],
            [primary_button("لغو", "admin_daily_digest")],
        ]
    )
    await callback.message.edit_text(
        "⚠️ اگر پیام امروز با وجود خطا در کانال منتشر شده باشد، تلاش دوباره آن را تکرار می‌کند.\n"
        "فقط پس از بررسی کانال ادامه بده.",
        reply_markup=keyboard,
        parse_mode=None,
    )


@router.callback_query(F.data == "admin_daily_digest:retry")
async def retry_daily_digest(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("تلاش دوباره فقط در گفت‌وگوی خصوصی در دسترس است.", show_alert=True)
        return
    result = await DailyChannelDigestService.retry_failed_today(bot)
    if result.status == "sent":
        await callback.answer("پست امروز ارسال شد.")
    elif result.status == "failed":
        await callback.answer("ارسال ناموفق بود؛ جزئیات در وضعیت پنل ثبت شد.", show_alert=True)
    elif result.status == "skipped":
        await callback.answer(preview_reason_fa(result.reason), show_alert=True)
    else:
        await callback.answer("موردی برای تلاش دوباره پیدا نشد.", show_alert=True)
    text, keyboard = await _panel()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)
