"""Admin meta panel (Phase 16): characters, shop, economy, garden, balance
lab, analytics and security review — private, admin-only.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import func, select

from sib.keyboards.common import danger_button, primary_button
from sib.admin.auth import IsAdminFilter
from sib.database.connection import get_session
from sib.database.models import (
    AppleBattle,
    AppleTransaction,
    Character,
    GardenPlot,
    PanelOwner,
    PlayerCard,
    SeedlingInventory,
    ShopRotation,
    Trade,
    User,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.balance_lab import BalanceLabService
from sib.services.characters import CharacterService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="admin_meta")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


MAIN_MENU = _kb(
    [
        [
            primary_button(text="👤 مدیریت شخصیت‌ها", callback_data="adm_chars"),
            primary_button(text="🏪 مدیریت فروشگاه", callback_data="adm_shop"),
        ],
        [
            primary_button(text="🍎 مدیریت اقتصاد", callback_data="adm_econ"),
            primary_button(text="🌳 مدیریت باغ", callback_data="adm_garden"),
        ],
        [
            primary_button(text="⚖️ آزمایشگاه بالانس", callback_data="adm_lab"),
            primary_button(text="📊 تحلیل‌ها", callback_data="adm_stats"),
        ],
        [
            primary_button(text="🛡 بازبینی امنیتی", callback_data="adm_security"),
            primary_button(text="🔄 بروزرسانی", callback_data="adm_menu"),
        ],
    ]
)


async def _analytics_text() -> str:
    async with get_session() as session:
        users = await session.scalar(select(func.count(User.id)))
        chars = await session.scalar(select(func.count(Character.id)))
        cards = await session.scalar(select(func.count(PlayerCard.id)))
        plots = await session.scalar(select(func.count(GardenPlot.id)))
        trades = await session.scalar(select(func.count(Trade.id)))
        battles = await session.scalar(select(func.count(AppleBattle.id)))
        tx_count = await session.scalar(select(func.count(AppleTransaction.id)))
        apples_total = await session.scalar(select(func.coalesce(func.sum(AppleWallet.balance), 0)))
        seedlings_total = await session.scalar(
            select(func.coalesce(func.sum(SeedlingInventory.total), 0))
        )
        rotations = await session.scalar(select(func.count(ShopRotation.id)))

    return (
        "📊 **تحلیل‌ها**\n\n"
        f"👤 کاربران: `{users}`\n"
        f"🎭 شخصیت‌ها: `{chars}` | 🎴 کارت‌ها: `{cards}`\n"
        f"🍎 سیب در گردش: `{apples_total}` در `{tx_count}` تراکنش\n"
        f"🌱 نهال‌های موجود: `{seedlings_total}`\n"
        f"🌳 قطعات باغ کاشته/خالی: `{plots}`\n"
        f"🔁 معاملات: `{trades}` | ⚔️ نبردهای سیبی: `{battles}`\n"
        f"🏪 چرخش‌های فروشگاه: `{rotations}`"
    )


async def _characters_text() -> str:
    async with get_session() as session:
        characters = await CharacterService.list_all(session)
    from sib.services.shop import ShopService

    card_price = ShopService.card_price()
    lines = ["👤 **مدیریت شخصیت‌ها**\n"]
    for c in characters:
        status = {"DRAFT": "📝", "RELEASED": "✅", "RETIRED": "🗄"}.get(c.release_status, "?")
        active = "" if c.is_active else " (غیرفعال)"
        lines.append(
            f"{status} `{c.id}` {c.name_fa} | "
            f"💪{c.power} ⚡{c.speed} 🧠{c.intelligence} | {card_price} 🍎 | v{c.balance_version}{active}"
        )
    lines.append(
        "\nشخصیت‌ها را از طریق داده‌ها (seed JSON / سرویس CharacterService) اضافه کنید — بدون تغییر کد."
    )
    lines.append(
        f"💰 قیمت همهٔ کارت‌های فروشگاه ثابت و سراسری است: {card_price} سیب."
    )
    return "\n".join(lines)


async def _shop_text() -> str:
    from sib.services.shop import ShopService

    async with get_session() as session:
        entries = await ShopService.list_entries(session)
        rotation = await ShopService.current_rotation(session)

    price = ShopService.card_price()
    lines = [
        "🏪 **مدیریت فروشگاه**\n",
        f"چرخش فعلی: `{rotation.week_id}` تا {rotation.ends_at}",
        f"💰 قیمت سراسری کارت‌ها: `{price}` سیب",
        "",
    ]
    for entry, character, _ in entries:
        badge = f" | {entry.badge}" if entry.badge else ""
        lines.append(f"🍎 {character.name_fa} — {price} سیب{badge}")
    lines.append("\nتنظیمات: shop_rotation_hours / shop_pool_character_ids در پیکربندی سیستم.")
    return "\n".join(lines)


async def _economy_text() -> str:
    async with get_session() as session:
        balance = await session.scalar(
            select(func.coalesce(func.sum(AppleWallet.balance), 0))
        )
        recent = (
            await session.scalars(
                select(AppleTransaction).order_by(AppleTransaction.id.desc()).limit(8)
            )
        ).all()
    lines = [
        "🍎 **مدیریت اقتصاد**\n",
        f"مجموع سیب در گردش: `{balance}`",
        "",
        "آخرین تراکنش‌ها (دفتر کل حسابرسی‌پذیر):",
    ]
    for tx in recent:
        lines.append(
            f"`{tx.id}` u{tx.user_id} | {tx.amount:+d} | {tx.reason} | مانده: {tx.balance_after}"
        )
    return "\n".join(lines)


async def _garden_text() -> str:
    async with get_session() as session:
        from sib.services.config_store import ConfigService

        growth = await ConfigService.get(session, "garden_growth_seconds")
        fruit = await ConfigService.get(session, "garden_fruit_seconds")
        amount = await ConfigService.get(session, "garden_fruit_amount")
        plots = await session.scalar(select(func.count(GardenPlot.id)))
    return (
        "🌳 **مدیریت باغ**\n\n"
        f"⏱ رشد: `{growth}` ثانیه | میوه‌دهی: `{fruit}` ثانیه | مقدار میوه: `{amount}` سیب\n"
        f"🌾 قطعات ثبت‌شده: `{plots}`\n\n"
        "همه مقادیر از پیکربندی سیستم (system_config) قابل تغییرند."
    )


async def _lab_text() -> str:
    async with get_session() as session:
        characters = await CharacterService.list_all(session)
        report = None
        if len(characters) >= 2:
            report = await BalanceLabService.simulate_characters(
                session, characters[0].id, characters[1].id, rounds=100_000, seed="balance-lab-demo"
            )

    lines = [
        "⚖️ **آزمایشگاه بالانس**\n",
        "شبیه‌سازی ۱۰۰٬۰۰۰ نبرد با مدل امتیازدهی واقعی (RNG مستند و تکرارپذیر):",
        "",
    ]
    if report:
        lines.append(
            f"{report['character_a']['name_fa']} ({report['score_a']}) در برابر "
            f"{report['character_b']['name_fa']} ({report['score_b']})"
        )
        lines.append(
            f"نرخ برد: {report['win_rate_a']:.1%} — {report['win_rate_b']:.1%} "
            f"(بذر: `{report['seed']}`)"
        )
        lines.append(
            f"نسخه بالانس: v{report['character_a']['balance_version']} / v{report['character_b']['balance_version']}"
        )
    else:
        lines.append("حداقل دو شخصیت لازم است.")
    return "\n".join(lines)


async def _security_text() -> str:
    async with get_session() as session:
        panels = await session.scalar(select(func.count(PanelOwner.id)))
    return (
        "🛡 **بازبینی امنیتی**\n\n"
        "✅ پنل‌های مدیریتی فقط مالک‌محور (PanelGuardMiddleware + OWNER_SCOPED_CALLBACKS)\n"
        "✅ پنل ادمین فقط در چت خصوصی و فقط برای ADMIN_PANEL_OWNER_ID\n"
        "✅ افزودن ادمین فقط با احراز هویت از طریق مالک در پیوی\n"
        "✅ سقف مجاز ایجاد گروه/پنل برای هر سازنده (flood threshold)\n"
        "✅ مسدودسازی خودکار سازندگان متجاوز و ثبت رویدادهای امنیتی\n"
        "✅ جلوگیری از دسترسی گروه‌ها به کالکشن/فروشگاه/تنظیمات شخصی/مالی\n"
        "✅ دفتر کل سیب با کلید idempotency و مانده‌ی بدون منفی\n"
        "✅ نبرد سیبی: نتیجه از پیش محاسبه و با بذر مستند ذخیره می‌شود\n"
        f"✅ پنل‌های ثبت‌شده مالک‌محور: `{panels}`\n\n"
        "موارد خارج از محدوده (در گزارش نهایی فهرست شده): محدودسازی ساخت پنل‌ها در گروه‌های خصوصی."
    )


@router.message(Command("admin"))
async def cmd_admin_meta(message: Message):
    if message.chat.type != "private":
        await message.reply("⚙️ پنل ادمین فقط در چت خصوصی در دسترس است.")
        return
    text = (
        "⚙️ **پنل مدیریت سیب**\n\n"
        "همه بخش‌ها را مدیریت کنید — داده‌محور و بدون تغییر کد."
    )
    await message.reply(text, reply_markup=MAIN_MENU, parse_mode="Markdown")


@router.callback_query(F.data.startswith("adm_"))
async def handle_admin_meta(callback: CallbackQuery, bot: Bot):
    action = callback.data.split(":")[0]
    back = _kb([[danger_button(text="🔙 بازگشت", callback_data="adm_menu")]])

    if action == "adm_menu":
        text = "⚙️ **پنل مدیریت سیب** — بخش موردنظر را انتخاب کنید."
        kb = MAIN_MENU
    elif action == "adm_chars":
        text, kb = await _characters_text(), back
    elif action == "adm_shop":
        text, kb = await _shop_text(), back
    elif action == "adm_econ":
        text, kb = await _economy_text(), back
    elif action == "adm_garden":
        text, kb = await _garden_text(), back
    elif action == "adm_lab":
        text, kb = await _lab_text(), back
    elif action == "adm_stats":
        text, kb = await _analytics_text(), back
    elif action == "adm_security":
        text, kb = await _security_text(), back
    else:
        await safe_answer_callback(callback)
        return

    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
