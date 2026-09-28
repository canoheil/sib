"""Start command and basic navigation handlers."""

from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sib.core.config import settings
from sib.core.errors import MESSAGES
from sib.core.logging import logger, log_event, Events
from sib.database.connection import get_session
from sib.keyboards.common import (
    get_membership_keyboard,
    get_private_welcome_keyboard,
    get_group_welcome_keyboard,
    get_cards_keyboard,
    get_profile_keyboard,
    nav_back_row,
)
from sib.services.nav import back_label_for, back_target, parse_callback, with_origin
from sib.services.panel import PanelOwnershipService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
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
            await safe_edit_callback_message(
                callback,
                bot,
                welcome_text,
                reply_markup=get_private_welcome_keyboard(settings.BOT_USERNAME),
            )


HELP_TOPICS = {
    "rps": (
        "🎯 سنگ، کاغذ، قیچی",
        "🎯 **سنگ، کاغذ، قیچی**\n\n"
        "یکی از بازی‌های سریع سیب! دو گروه در برابر هم قرار می‌گیرند؛ "
        "هر بازیکن مخفیانه انتخابش را می‌زند و نتیجه لحظه‌ای اعلام می‌شود. "
        "برنده‌ی بیشترین دور، قهرمان مسابقه است.",
    ),
    "c4": (
        "🔴 چهار در یک ردیف",
        "🔴 **چهار در یک ردیف (Connect Four)**\n\n"
        "مهره‌ات را در یک ستون بنداز و قبل از حریف چهار مهره در یک ردیف (افقی، عمودی یا مورب) جور کن. "
        "حالت‌های انفرادی، ۲ به ۲ و ۴ به ۴ دارد.",
    ),
    "ttt": (
        "❌ دوز",
        "❌ **دوز (Tic-Tac-Toe)**\n\n"
        "کلاسیک و سریع! در یک جدول ۳ در ۳ سه نشانه در یک ردیف جور کن. "
        "بازی انفرادی (۱ به ۱) است.",
    ),
    "battleship": (
        "🚢 کشتی جنگی",
        "🚢 **کشتی جنگی (Battleship)**\n\n"
        "ناوگان دشمن را در مه دریا پیدا کن! به مختصات روی رادار شلیک کن؛ "
        "آبی = شلیک به آب، 💥 = اصابت، 🔥 = غرق شدن کشتی. "
        "حالت‌های انفرادی و تیمی دارد.",
    ),
    "high_card": (
        "🃏 کارت بالا",
        "🃏 **کارت بالا (High Card)**\n\n"
        "سریع‌ترین بازی سیب! هر بازیکن یک کارت می‌کشد؛ کارت بالاتر می‌برد. "
        "برای گرم‌کردن عالی است.",
    ),
    "apple_battle": (
        "⚔️ نبرد سیبی",
        "⚔️ **نبرد سیبی**\n\n"
        "با یکی از کارت‌هایت وارد صف نبرد شو؛ سیب یک حریف هم‌سطح پیدا می‌کند و "
        "نبرد به‌صورت خودکار انجام و روایت می‌شود. برنده نهال جایزه می‌گیرد! "
        "لیدربورد نبرد هم همان‌جاست.",
    ),
    "cards": (
        "🃏 کارت‌ها",
        "🃏 **کارت‌های من**\n\n"
        "کارت‌های شخصیتت را جمع کن، به‌روزرسانی‌شان کن و کارت اصلی‌ات را انتخاب کن. "
        "از فروشگاه کارت جدید بخر یا با دوستانت مبادله کن.",
    ),
    "garden": (
        "🌳 باغ",
        "🌳 **باغ سیب**\n\n"
        "در قطعه‌های باغت نهال بکار، صبر کن تا درخت شود و میوه‌اش را برداشت کن. "
        "هنگام برداشت شانسِ افتادن سیب‌های کمیاب را هم داری: "
        "🍎 معمولی، 🥇 طلایی و 🖤 ابسیدینی! "
        "باغت را همه می‌بینند ولی فقط خودت کنترلش می‌کنی.",
    ),
}


def _help_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row: list = []
    for topic, (label, _text) in HELP_TOPICS.items():
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"help_page:{topic}")
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "help_menu")
async def handle_help_menu(callback: CallbackQuery, bot: Bot):
    """Paged help index (spec #13): short pages, button navigation."""
    text = (
        "🎮 **راهنمای بازی‌های سیب** 🍏\n\n"
        "بخش مورد نظرت را انتخاب کن تا کوتاه و ساده توضیحش را ببینی:"
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=_help_keyboard())


@router.callback_query(F.data.startswith("help_page:"))
async def handle_help_page(callback: CallbackQuery, bot: Bot):
    topic = callback.data.split(":")[1]
    entry = HELP_TOPICS.get(topic)
    if not entry:
        await safe_answer_callback(callback, "این بخش پیدا نشد 🍏", show_alert=True)
        return
    _label, text = entry
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 بازگشت به راهنما", callback_data="help_menu")],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="main_menu")],
        ]
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


async def _claim_panel(
    callback: CallbackQuery,
    panel_type: str,
    previous_message_id: Optional[int] = None,
) -> None:
    """Bind a personal panel message to its opener (DB, authoritative).

    ``previous_message_id`` is the message id BEFORE the screen render: when
    the render replaced the message (text↔photo transition), the claim is
    MOVED to the new message — a deleted message_id must never linger in
    the panel registry.
    """
    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    if chat is None or not msg_id or not callback.from_user:
        return
    if not isinstance(msg_id, int):
        return
    async with get_session() as session:
        if (
            isinstance(previous_message_id, int)
            and previous_message_id != msg_id
        ):
            await PanelOwnershipService.move_claim(
                session,
                chat_id=chat.id,
                old_message_id=previous_message_id,
                new_message_id=msg_id,
                user_id=callback.from_user.id,
                panel_type=panel_type,
            )
        else:
            await PanelOwnershipService.claim(
                session,
                chat_id=chat.id,
                message_id=msg_id,
                user_id=callback.from_user.id,
                panel_type=panel_type,
            )
        await session.commit()


@router.callback_query(F.data == "apple_garden")
async def handle_apple_garden(callback: CallbackQuery, bot: Bot):
    """Open the sender's garden anywhere (public view + private control)."""
    from sib.handlers.garden import _render_garden
    from sib.services.banner import render_banner

    user = callback.from_user
    if not user:
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        text, kb = await _render_garden(user.id, session)

    await callback.answer()
    if callback.message:
        await render_banner(callback, bot, "garden", text, kb)
    await _claim_panel(callback, "garden", prev_message_id)


@router.callback_query(F.data.startswith("apple_cards"))
async def handle_apple_cards(callback: CallbackQuery, bot: Bot):
    from sib.services.wiki import CollectionService

    user = callback.from_user
    if not user:
        return
    _base, origin = parse_callback(callback.data)

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        text = await CollectionService.render_collection(session, user.id)

    kb = get_cards_keyboard(origin=origin)

    await callback.answer()
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            text,
            reply_markup=kb,
        )
    await _claim_panel(callback, "cards", prev_message_id)


@router.callback_query(F.data == "apple_shop")
async def handle_apple_shop(callback: CallbackQuery, bot: Bot):
    from sib.handlers.shop import _shop_view
    from sib.services.banner import render_banner

    user = callback.from_user
    if not user:
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    text, kb = await _shop_view(user.id)

    await callback.answer()
    if callback.message:
        await render_banner(callback, bot, "shop", text, kb)
    await _claim_panel(callback, "shop", prev_message_id)


@router.callback_query(F.data.startswith("apple_battle"))
async def handle_apple_battle(callback: CallbackQuery, bot: Bot):
    from sib.handlers.apple_battle import battle_entry_view

    user = callback.from_user
    if not user:
        return
    _base, origin = parse_callback(callback.data)

    text, kb = await battle_entry_view(user.id, origin=origin)

    await callback.answer()
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            text,
            reply_markup=kb,
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

    prev_message_id = getattr(callback.message, "message_id", None)
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
        fav_photo = None
        for card in cards:
            if getattr(card, "is_favorite", False):
                character = await session.get(
                    __import__("sib.database.models", fromlist=["Character"]).Character,
                    card.character_id,
                )
                if character:
                    fav_text = character.name_fa
                    from sib.services.character_art import (
                        photo_arg,
                        resolve_character_photo,
                    )

                    file_id, fs_path = await resolve_character_photo(session, character)
                    fav_photo = photo_arg(file_id, fs_path)
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

    kb = get_profile_keyboard()

    await callback.answer()
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            "\n".join(lines),
            reply_markup=kb,
            photo=fav_photo,
        )
    await _claim_panel(callback, "profile", prev_message_id)


def _main_menu_text_and_kb(user, chat) -> tuple:
    """Shared main-menu builder (context-aware): groups get the group menu,
    private chats get the private menu. Used by both the 'main_menu'
    callback and the 'سیب منو' wake word - one source of truth."""
    is_group = bool(chat and chat.type in ("group", "supergroup"))
    if is_group:
        text = "🍏 **منوی اصلی سیب**\nاز منوی زیر بخش مورد نظرت رو انتخاب کن 👇"
        kb = get_group_welcome_keyboard()
    else:
        name = user.first_name if user else "دوست"
        text = (
            f"🍏 **منوی اصلی سیب**\n\n"
            f"سلام {name}! از منوی زیر بخش مورد نظرت رو انتخاب کن 👇"
        )
        kb = get_private_welcome_keyboard(settings.BOT_USERNAME)
    return text, kb


async def _reply_main_menu(message: Message) -> None:
    from sib.services.banner import send_screen_reply

    text, kb = _main_menu_text_and_kb(message.from_user, message.chat)
    await send_screen_reply(message, "main_menu", text, kb)


@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, bot: Bot, state: FSMContext = None):
    """Context-aware main menu: one button works everywhere.

    Navigating home ALWAYS exits any active FSM flow (admin flows etc.).
    The screen is rendered by the same canonical path as the 'سیب منو'
    wake word (banner + caption + keyboard).
    """
    from sib.services.banner import render_banner

    if state is not None:
        await state.clear()
    text, kb = _main_menu_text_and_kb(
        callback.from_user, callback.message.chat if callback.message else None
    )
    await safe_answer_callback(callback)
    if callback.message:
        await render_banner(callback, bot, "main_menu", text, kb)


async def _reply_games_menu(message: Message) -> None:
    from sib.handlers.party.handlers import build_games_menu
    from sib.services.banner import send_screen_reply

    async with get_session() as session:
        if message.from_user:
            await UserService.get_or_create_user(session, message.from_user)
        await GroupService.get_or_create_group(session, message.chat)
        text, kb = await build_games_menu(session, message.chat.id)

    await send_screen_reply(message, "games", text, kb)


@router.message(F.text.regexp(r"^\s*سیب\s+(بازی|منو)\s*$"))
async def handle_sib_games_word(message: Message, bot: Bot):
    """'سیب بازی' / 'سیب منو' routing.

    PRIVATE:   سیب منو -> Main Menu | سیب بازی -> Games Menu
    GROUP:     سیب منو -> GROUP Main Menu | سیب بازی -> Games Menu

    Distinct regexes: these NEVER fall through to the 'سیب' claim action.
    """
    text_lower = (message.text or "").strip()
    if text_lower.endswith("منو"):
        await _reply_main_menu(message)
    else:
        await _reply_games_menu(message)


@router.message(F.text.regexp(r"^\s*(سیبام|سیب\s+من)\s*$"))
async def handle_sib_balance_word(message: Message, bot: Bot):
    """'سیبام' / 'سیب من' -> show the SENDER's apple balance.

    Uses the authoritative AppleWalletService balance (group + private).
    Never falls through to the 'سیب' claim action.
    """
    user = message.from_user
    if not user:
        return

    from sib.services.apple_wallet import AppleWalletService

    async with get_session() as session:
        from sib.services.user import UserService

        await UserService.get_or_create_user(session, user)
        balance = await AppleWalletService.get_balance(session, user.id)

    await message.reply(f"🍏 موجودی سیب شما: {balance}", parse_mode="Markdown")


@router.message(F.text.regexp(r"^\s*سیب\s+پروفایل\s*$"))
async def handle_sib_profile_word(message: Message, bot: Bot):
    """'سیب پروفایل' opens the PROFILE OF THE SENDER (spec #4).

    The subject is the message sender - never previous panel state.
    """
    user = message.from_user
    if not user:
        return

    from sqlalchemy import func, select as sa_select

    from sib.database.models import AppleBattle, AppleBattleStatus
    from sib.services.apple_wallet import AppleWalletService
    from sib.services.player_cards import PlayerCardService
    from sib.services.seedling import SeedlingService

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        cards = await PlayerCardService.list_cards(session, user.id)
        seedlings = await SeedlingService.get_total(session, user.id)
        apples = await AppleWalletService.get_balance(session, user.id)
        wins = await session.scalar(
            sa_select(func.count(AppleBattle.id))
            .where(AppleBattle.winner_id == user.id)
            .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
        )
        battles = await session.scalar(
            sa_select(func.count(AppleBattle.id)).where(
                (
                    (AppleBattle.player1_id == user.id)
                    | (AppleBattle.player2_id == user.id)
                )
                & (AppleBattle.status == AppleBattleStatus.COMPLETED)
            )
        )
        fav_text = None
        fav_photo = None
        for card in cards:
            if getattr(card, "is_favorite", False):
                from sib.database.models import Character

                character = await session.get(Character, card.character_id)
                if character:
                    fav_text = character.name_fa
                    from sib.services.character_art import (
                        photo_arg,
                        resolve_character_photo,
                    )

                    file_id, fs_path = await resolve_character_photo(session, character)
                    fav_photo = photo_arg(file_id, fs_path)
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
    profile_text = "\n".join(lines)
    profile_kb = get_profile_keyboard()
    sent = None
    if fav_photo is not None:
        try:
            sent = await message.reply_photo(
                fav_photo, caption=profile_text, reply_markup=profile_kb, parse_mode="Markdown"
            )
        except Exception:
            sent = None
    if sent is None:
        sent = await message.reply(
            profile_text, reply_markup=profile_kb, parse_mode="Markdown"
        )
    if sent is not None and sent.message_id:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="profile",
            )
            await session.commit()


async def _open_sender_garden(message: Message) -> None:
    """Open the SENDER's garden in the current chat (public view + private
    control). Single shared path for 'سیب باغ' and 'باغ من'.

    The subject is always the message sender - never callback/memory
    state - and the opened panel is claimed to the sender in the DB.
    """
    user = message.from_user
    if not user:
        return

    from sib.handlers.garden import _render_garden
    from sib.services.banner import send_screen_reply

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        text, kb = await _render_garden(user.id, session)
        sent = await send_screen_reply(message, "garden", text, kb)
        if sent is not None and sent.message_id:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="garden",
            )


@router.message(F.text.regexp(r"^\s*سیب\s+باغ\s*$"))
async def handle_sib_garden_word(message: Message, bot: Bot):
    """'سیب باغ' -> the sender's garden in the current chat."""
    await _open_sender_garden(message)


@router.message(F.text.regexp(r"^\s*باغ\s+من\s*$"))
async def handle_my_garden_word(message: Message, bot: Bot):
    """'باغ من' behaves exactly like 'سیب باغ' (sender's own garden)."""
    await _open_sender_garden(message)


@router.message(F.text.regexp(r"^\s*سیب\s*$"))
async def handle_sib_wake_word(message: Message, bot: Bot):
    """'سیب' in groups is a REAL action: claim apples (task #4).

    Per-user cooldown; the award is persisted to the wallet. This no
    longer opens any menu - use 'سیب بازی' for the games menu.
    """
    if message.chat.type not in ("group", "supergroup"):
        return

    user = message.from_user
    if not user:
        return

    from sib.services.sib_claim import SibClaimService

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        text, _awarded = await SibClaimService.claim(session, user.id)

    await message.reply(text)
