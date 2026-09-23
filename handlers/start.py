"""Start command and basic navigation handlers."""

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from goje.core.config import settings
from goje.core.errors import MESSAGES
from goje.core.logging import logger, log_event, Events
from goje.database.connection import get_session
from goje.keyboards.common import (
    get_membership_keyboard,
    get_private_welcome_keyboard,
    get_group_welcome_keyboard,
)
from goje.services.membership.checker import MembershipService
from goje.services.user import UserService
from goje.services.group import GroupService

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
                f"🍅 **گوجه وارد شد! سلام به برو بچِ {chat.title}!**\n\n"
                f"گروه شما ثبت شد و ریتینگ رقابتی اولیه **{settings.INITIAL_RATING:g}** براتون لحاظ شد.\n\n"
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
                "🍅 **سلام!**\n\n"
                "برای استفاده از گوجه و دریافت اطلاعیه‌ها و بازی با گروه‌ها، عضو کانال رسمی شو 👇"
            )
            await message.answer(
                membership_text,
                reply_markup=get_membership_keyboard(channel_link),
                parse_mode="Markdown",
            )
            return

        # If already member
        welcome_text = (
            f"🍅 **سلام {user.first_name} عزیز! من گوجه‌ام!**\n\n"
            "من ربات بازی گروهی آنلاین تلگرام هستم.\n"
            "من رو به گروه‌هات اضافه کن، تیم تشکیل بده و با گروه‌های دیگه در سراسر تلگرام مسابقه بده!\n\n"
            "🎮 بازی‌های فعال:\n"
            "• کارت بالا (High Card)\n"
            "• سنگ، کاغذ، قیچی (Rock Paper Scissors)\n"
            "• دوز (Tic-Tac-Toe)\n"
            "• چهار در یک ردیف (Connect Four - ۱v۱, ۲v۲, ۴v۴)\n"
            "• کشتی جنگی (Battleship - ۱v۱, ۲v۲, ۴v۴)\n\n"
            "🏆 هر گروه تلگرام ریتینگ سراسری خودشو داره!\n"
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
            f"🍅 **سلام {user.first_name}! عضویتت تایید شد!**\n\n"
            "به جمع گوجه‌ای‌ها خوش اومدی!\n"
            "گوجه رو به گروه‌تون اضافه کن و چالش بازی‌های تیمی رو آغاز کنید 👇"
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
        "🍅 **راهنمای بازی‌های گوجه**\n\n"
        "۱. گوجه رو به گروه تلگرامی خودت اضافه کن.\n"
        "۲. توی گروه دکمه «بازی‌های فعال گروه» رو بزن یا بازی مورد علاقه‌ت رو انتخاب کن.\n"
        "۳. بازی‌های انفرادی (Solo) یا تیمی (2v2 و 4v4) وجود داره.\n"
        "۴. بعد از تکمیل شدن اعضای تیم، دکمه «پیدا کردن حریف» رو بزن.\n"
        "۵. گوجه یک گروه حریف متناسب با ریتینگ شما از گروه‌های دیگه تلگرام پیدا می‌کنه!\n"
        "۶. بازی مستقیماً داخل گروه خودتون انجام می‌شه و نیازی به رفتن به گروه دیگه نیست.\n\n"
        "🏆 با برد مسابقه، ریتینگ گروهتون در جدول برترین گروه‌ها بالا می‌ره!"
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(help_text, parse_mode="Markdown")


@router.message(F.text.regexp(r"^\s*گوجه\s*$"))
async def handle_goje_wake_word(message: Message, bot: Bot):
    """Handle the exact 'گوجه' wake word in group chats."""
    if message.chat.type not in ("group", "supergroup"):
        return

    # Reuse the existing /start handler so behavior stays identical.
    await handle_start(message, bot)
