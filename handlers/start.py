"""Start command and basic navigation handlers."""

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sib.core.config import settings
from sib.core.errors import MESSAGES
from sib.core.logging import logger, log_event, Events
from sib.database.connection import get_session
from sib.keyboards.common import (
    get_membership_keyboard,
    get_private_welcome_keyboard,
    get_group_welcome_keyboard,
)
from sib.services.membership.checker import MembershipService
from sib.services.user import UserService
from sib.services.group import GroupService

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message, bot: Bot):
    """Handle /start command in private and group chats."""
    user = message.from_user
    chat = message.chat

    if not user:
        return

    async with get_session() as session:
        # Register user
        await UserService.get_or_create_user(session, user)

        # 1. Group / Supergroup chat
        if chat.type in ("group", "supergroup"):
            group = await GroupService.get_or_create_group(session, chat)
            welcome_text = (
                f"🍏 **سیب وارد شد! سلام به برو بچِ {chat.title}!**\n\n"
                f"گروه شما ثبت شد و از این پس با هر برد، یک **برد** به افتخارات گروه‌تون اضافه می‌شه.\n\n"
                f"برای ساخت پارتی بازی و مبارزه با گروه‌های دیگه، دکمه‌های زیر رو لمس کنید 👇"
            )
            await message.reply(welcome_text, reply_markup=get_group_welcome_keyboard(), parse_mode="Markdown")
            return

        # 2. Private chat
        # Check channel membership gate
        is_member, missing_channel = await MembershipService.check_user_membership(bot, user.id, session)

        if not is_member:
            channel_link = missing_channel.invite_link if missing_channel else settings.OFFICIAL_CHANNEL_LINK
            membership_text = (
                "🍏 **سلام!**\n\n"
                "برای استفاده از سیب و دریافت اطلاعیه‌ها و بازی با گروه‌ها، عضو کانال رسمی شو 👇"
            )
            await message.answer(
                membership_text,
                reply_markup=get_membership_keyboard(channel_link),
                parse_mode="Markdown",
            )
            return

        # If already member
        welcome_text = (
            f"🍏 **سلام {user.first_name} عزیز! من سیب‌ام!**\n\n"
            "من ربات بازی گروهی آنلاین تلگرام هستم.\n"
            "من رو به گروه‌هات اضافه کن، تیم تشکیل بده و با گروه‌های دیگه در سراسر تلگرام مسابقه بده!\n\n"
            "🎮 بازی‌های فعال:\n"
            "• کارت بالا (High Card)\n"
            "• سنگ، کاغذ، قیچی (Rock Paper Scissors)\n"
            "• دوز (Tic-Tac-Toe)\n"
            "• چهار در یک ردیف (Connect Four - ۱v۱, ۲v۲, ۴v۴)\n"
            "• کشتی جنگی (Battleship - ۱v۱, ۲v۲, ۴v۴)\n\n"
            "🏆 هر گروه تلگرام با تعداد بردهاش در جدول برترین‌ها قرار می‌گیره!\n"
            "برای شروع دکمه زیر رو لمس کن:"
        )
        await message.answer(
            welcome_text,
            reply_markup=get_private_welcome_keyboard(settings.BOT_USERNAME),
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "check_membership")
async def handle_check_membership(callback: CallbackQuery, bot: Bot):
    """Handle user clicking [✅ بررسی عضویت]."""
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        is_member, missing_channel = await MembershipService.check_user_membership(bot, user.id, session)

        if not is_member:
            await callback.answer(MESSAGES["membership_check_failed"], show_alert=True)
            return

        await callback.answer(MESSAGES["membership_check_success"], show_alert=True)
        
        # Replace gate message with welcome
        welcome_text = (
            f"🍏 **سلام {user.first_name}! عضویتت تایید شد!**\n\n"
            "به جمع سیب‌ای‌ها خوش اومدی!\n"
            "سیب رو به گروه‌تون اضافه کن و چالش بازی‌های تیمی رو آغاز کنید 👇"
        )
        if callback.message:
            await callback.message.edit_text(
                welcome_text,
                reply_markup=get_private_welcome_keyboard(settings.BOT_USERNAME),
                parse_mode="Markdown",
            )


@router.callback_query(F.data == "help_menu")
async def handle_help_menu(callback: CallbackQuery):
    """Show help menu in private chat."""
    help_text = (
        "🍏 **راهنمای بازی‌های سیب**\n\n"
        "۱. سیب رو به گروه تلگرامی خودت اضافه کن.\n"
        "۲. توی گروه دکمه «بازی‌های فعال گروه» رو بزن یا بازی مورد علاقه‌ت رو انتخاب کن.\n"
        "۳. بازی‌های انفرادی (Solo) یا تیمی (2v2 و 4v4) وجود داره.\n"
        "۴. بعد از تکمیل شدن اعضای تیم، دکمه «پیدا کردن حریف» رو بزن.\n"
        "۵. سیب یک گروه حریف هم‌سطح از گروه‌های دیگه تلگرام پیدا می‌کنه!\n"
        "۶. بازی مستقیماً داخل گروه خودتون انجام می‌شه و نیازی به رفتن به گروه دیگه نیست.\n\n"
        "🏆 با برد مسابقه، تعداد بردهای گروه‌تون در جدول برترین گروه‌ها بالا می‌ره!"
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(help_text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Apple Ecosystem main-menu navigation
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "apple_garden")
async def handle_apple_garden(callback: CallbackQuery, bot: Bot):
    if callback.message and callback.message.chat.type != "private":
        await callback.answer(
            "🌳 باغ شخصی فقط در چت خصوصی قابل مدیریت است؛ به پیوی سیب بیایید!",
            show_alert=True,
        )
        return

    from sib.handlers.garden import _render_garden

    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        text, kb = await _render_garden(user.id, session)

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "apple_cards")
async def handle_apple_cards(callback: CallbackQuery, bot: Bot):
    from sib.services.wiki import CollectionService

    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        text = await CollectionService.render_collection(session, user.id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 ویکی کاراکترها",
                    callback_data="apple_wiki",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
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


@router.callback_query(F.data == "apple_shop")
async def handle_apple_shop(callback: CallbackQuery, bot: Bot):
    from sib.handlers.shop import _shop_view

    user = callback.from_user
    if not user:
        return

    text, kb = await _shop_view(user.id)

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "apple_battle")
async def handle_apple_battle(callback: CallbackQuery, bot: Bot):
    from sib.handlers.apple_battle import _queue_view

    user = callback.from_user
    if not user:
        return

    text, kb = await _queue_view(user.id)

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "apple_profile")
async def handle_apple_profile(callback: CallbackQuery, bot: Bot):
    from sqlalchemy import func, select

    from sib.database.models import AppleBattle, AppleBattleStatus
    from sib.services.apple_wallet import AppleWalletService
    from sib.services.seedling import SeedlingService
    from sib.services.player_cards import PlayerCardService

    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        cards = await PlayerCardService.list_cards(session, user.id)
        seedlings = await SeedlingService.get_total(session, user.id)
        apples = await AppleWalletService.get_balance(session, user.id)

        wins = await session.scalar(
            select(func.count(AppleBattle.id))
            .where(AppleBattle.winner_id == user.id)
            .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
        )

        battles = await session.scalar(
            select(func.count(AppleBattle.id)).where(
                (
                    (AppleBattle.player1_id == user.id)
                    | (AppleBattle.player2_id == user.id)
                )
                & (AppleBattle.status == AppleBattleStatus.COMPLETED)
            )
        )

        fav_text = None
        for card in cards:
            if getattr(card, "is_favorite", False):
                character = await session.get(
                    __import__("sib.database.models", fromlist=["Character"]).Character,
                    card.character_id,
                )
                if character:
                    fav_text = character.name_fa
                break

    lines = [
        f"👤 **پروفایل سیبی {user.first_name or 'شما'}** 🍏",
        "",
        f"🍎 سیب: `{apples}`",
        f"🌱 نهال: `{seedlings}`",
        f"🎴 تعداد کارت‌ها: `{len(cards)}`",
        fav_text.strip() if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎴 کارت‌های من",
                    callback_data="apple_cards",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌳 باغ من",
                    callback_data="apple_garden",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚔️ نبرد سیبی",
                    callback_data="apple_battle",
                )
            ],
        ]
    )

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=kb,
            parse_mode="Markdown",
        )


@router.message(F.text.regexp(r"^\s*سیب\s*$"))
async def handle_sib_wake_word(message: Message, bot: Bot):
    """Handle the exact 'سیب' wake word in group chats."""
    if message.chat.type not in ("group", "supergroup"):
        return

    # Reuse the existing /start handler so behavior stays identical.
    await handle_start(message, bot)
