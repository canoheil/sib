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
    get_profile_skin_keyboard,
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
            "• چهار در یک ردیف (Connect Four؛ ۱v۱، ۲v۲، ۴v۴)\n"
            "• کشتی جنگی (Battleship؛ ۱v۱، ۲v۲، ۴v۴)\n\n"
            "🏆 هر گروه تلگرام با تعداد برده‌هاش تو جدول برترین‌ها قرار می‌گیره!\n"
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
            f"🍏 **سلام {user.first_name}! عضویتت تأیید شد!**\n\n"
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
        "یکی از بازی‌های سریع سیب! دو گروه روبه‌روی هم می‌ایستند؛ "
        "هر بازیکن مخفیانه انتخابش رو می‌زنه و نتیجه همون لحظه اعلام می‌شه. "
        "هر کی بیشترین دور رو ببره، قهرمان مسابقه‌ست.",
    ),
    "c4": (
        "🔴 چهار در یک ردیف",
        "🔴 **چهار در یک ردیف (Connect Four)**\n\n"
        "مهره‌ات رو تو یه ستون بنداز و قبل از حریف، چهار تا مهره رو در یک ردیف "
        "(افقی، عمودی یا مورب) جور کن. حالت‌های انفرادی، ۲ به ۲ و ۴ به ۴ هم داره.",
    ),
    "ttt": (
        "❌ دوز",
        "❌ **دوز (Tic-Tac-Toe)**\n\n"
        "کلاسیک و سریع! تو یه جدول ۳ در ۳، سه تا نشانه رو در یک ردیف جور کن. "
        "فقط حالت انفرادی (۱ به ۱) داره.",
    ),
    "battleship": (
        "🚢 کشتی جنگی",
        "🚢 **کشتی جنگی (Battleship)**\n\n"
        "ناوگان دشمن رو تو مه دریا پیدا کن! به مختصات روی رادار شلیک کن؛ "
        "آبی یعنی شلیک به آب، 💥 یعنی اصابت و 🔥 یعنی کشتی غرق شده. "
        "حالت انفرادی و تیمی داره.",
    ),
    "high_card": (
        "🃏 کارت بالا",
        "🃏 **کارت بالا (High Card)**\n\n"
        "سریع‌ترین بازی سیب! هر بازیکن یه کارت می‌کشه و کارت بالاتر می‌بره. "
        "برای گرم‌کردن عالیه.",
    ),
    "apple_battle": (
        "⚔️ نبرد سیبی",
        "⚔️ **نبرد سیبی**\n\n"
        "با یکی از کارت‌هات وارد صف نبرد شو؛ سیب یه حریف برات پیدا می‌کنه و "
        "نبرد خودکار انجام و روایت می‌شه. برنده نهال جایزه می‌گیره! "
        "لیدربورد نبرد هم همون‌جاست.",
    ),
    "cards": (
        "🃏 کارت‌ها",
        "🃏 **کارت‌های من**\n\n"
        "کارت‌های شخصیتت رو جمع کن، ارتقاشون بده و کارت اصلی‌ت رو انتخاب کن. "
        "از فروشگاه کارت جدید بخر یا با دوستات مبادله کن.",
    ),
    "garden": (
        "🌳 باغ",
        "🌳 **باغ سیب**\n\n"
        "تو قطعه‌های باغت نهال بکار، صبر کن تا درخت بشه و میوه‌ش رو برداشت کن. "
        "موقع برداشت شانس افتادن سیب‌های کمیاب رو هم داری: "
        "🍎 معمولی، 🌕 طلایی و 🌑 ابسیدینی! "
        "باغت رو همه می‌بینن ولی فقط خودت کنترلش می‌کنی.",
    ),
}


def _help_keyboard(origin: Optional[str] = None) -> InlineKeyboardMarkup:
    from sib.keyboards.common import danger_button, primary_button
    from sib.services.nav import with_origin

    buttons = []
    row: list = []
    for topic, (label, _text) in HELP_TOPICS.items():
        row.append(
            primary_button(
                text=label,
                callback_data=with_origin(f"help_page:{topic}", origin),
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([danger_button(text="🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("help_menu"))
async def handle_help_menu(callback: CallbackQuery, bot: Bot):
    """Paged help index (spec #13): short pages, button navigation.

    The Guide is reachable from the group menu and the private menu; the
    contextual origin is carried through the pages so Back returns to the
    screen that opened it. Back only navigates - it never touches an
    active match or party.
    """
    from sib.services.nav import parse_callback

    _base, origin = parse_callback(callback.data)
    text = (
        "🎮 **راهنمای بازی‌های سیب** 🍏\n\n"
        "بخش مورد نظرت را انتخاب کن تا کوتاه و ساده توضیحش را ببینی:"
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback, bot, text, reply_markup=_help_keyboard(origin)
    )


@router.callback_query(F.data.startswith("help_page:"))
async def handle_help_page(callback: CallbackQuery, bot: Bot):
    from sib.services.nav import parse_callback, with_origin

    # ``help_page:<topic>[:origin]`` — the topic is a HANDLER argument and
    # must never be eaten as the navigation origin (topics like
    # ``apple_battle`` share their name with a known screen callback).
    base, origin = parse_callback(callback.data, arg_segments=1)
    parts = base.split(":")
    topic = parts[1] if len(parts) > 1 else None
    entry = HELP_TOPICS.get(topic)
    if not entry:
        await safe_answer_callback(callback, "این بخش پیدا نشد 🍏", show_alert=True)
        return
    _label, text = entry
    from sib.keyboards.common import danger_button

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                danger_button(
                    text="📚 بازگشت به راهنما",
                    callback_data=with_origin("help_menu", origin),
                )
            ],
            [danger_button(text="🏠 منوی اصلی", callback_data="main_menu")],
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

    # Optional page argument: apple_cards[:page[:origin]]
    parts = _base.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        from sqlalchemy import select

        from sib.database.models import Character
        from sib.services.player_cards import PlayerCardService

        text, total_pages = await CollectionService.render_collection(session, user.id, page=page)
        cards = await PlayerCardService.list_cards(session, user.id)
        main_card = await PlayerCardService.get_main_card(session, user.id)
        main_card_id = main_card.id if main_card is not None else None
        character_ids = sorted({c.character_id for c in cards})
        characters = {
            ch.id: ch
            for ch in (
                await session.scalars(
                    select(Character).where(Character.id.in_(character_ids))
                )
            ).all()
        }

    card_rows = []
    page_cards = cards[
        page * CollectionService.PAGE_SIZE : (page + 1) * CollectionService.PAGE_SIZE
    ]
    for card in page_cards:
        character = characters.get(card.character_id)
        name = character.name_fa if character else "کارت"
        star = "⭐ " if card.id == main_card_id else ""
        card_rows.append((card.id, f"🍎 {star}{name} — سطح {card.level}"))

    kb = get_cards_keyboard(
        origin=origin, card_rows=card_rows, page=page, total_pages=total_pages
    )

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

    prev_message_id = getattr(callback.message, "message_id", None)
    text, kb = await battle_entry_view(user.id, origin=origin)

    await callback.answer()
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            text,
            reply_markup=kb,
        )
    await _claim_panel(callback, "apple_battle", prev_message_id)


@router.callback_query(F.data == "apple_profile")
async def handle_apple_profile(callback: CallbackQuery, bot: Bot):
    from sqlalchemy import func, select

    from sib.database.models import AppleBattle, AppleBattleStatus
    from sib.services.apple_wallet import AppleWalletService
    from sib.services.seedling import SeedlingService
    from sib.services.player_cards import PlayerCardService
    from sib.services.player_xp import PlayerXPService
    from sib.services.profile_skin import ProfileSkinService, profile_skin_label

    user = callback.from_user
    if not user:
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        cards = await PlayerCardService.list_cards(session, user.id)
        main_card = await PlayerCardService.get_main_card(session, user.id)
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
        from sib.database.models import Character

        if main_card is not None:
            character = await session.get(Character, main_card.character_id)
            if character:
                fav_text = character.name_fa
                from sib.services.character_art import (
                    photo_arg,
                    resolve_character_photo,
                )

                file_id, fs_path = await resolve_character_photo(session, character)
                fav_photo = photo_arg(file_id, fs_path)

        # The ACTIVE spouse + shared Love, straight from the marriage domain.
        from sib.handlers.marriage import profile_marriage_lines

        marriage_lines = await profile_marriage_lines(session, user.id)
        account_progress = await PlayerXPService.get_progress(session, user.id)
        selected_skin = await ProfileSkinService.get_selected_skin(session, user.id)

    lines = [
        f"👤 **پروفایل سیبی {user.first_name or 'شما'}** 🍏",
        PlayerXPService.format_profile_progress(account_progress),
        f"🎨 **Change Skin / تغییر پوسته**: {profile_skin_label(selected_skin)}",
        "",
        f"🍎 سیب: `{apples}`",
        f"🌱 نهال: `{seedlings}`",
        f"🎴 تعداد کارت‌ها: `{len(cards)}`",
        fav_text.strip() if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    lines.extend(marriage_lines)

    kb = get_profile_keyboard()

    # Green Apple delegates to its original Level-specific art resolver.
    from sib.services.profile_artwork import ProfileArtworkResolver

    main_photo = ProfileArtworkResolver.profile_photo(
        account_progress, skin=selected_skin, fallback_photo=fav_photo
    )

    await callback.answer()
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            "\n".join(lines),
            reply_markup=kb,
            photo=main_photo,
        )
    await _claim_panel(callback, "profile", prev_message_id)


@router.callback_query(F.data == "profile_skin")
async def handle_profile_skin(callback: CallbackQuery, bot: Bot):
    """Open the Change Skin choices for the profile owner."""
    user = callback.from_user
    if not user:
        return

    from sib.services.profile_skin import ProfileSkinService, profile_skin_label

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        # The selector can also repair an account first seen through a
        # profile callback, following the existing account-registration path.
        await UserService.get_or_create_user(session, user)
        selected_skin = await ProfileSkinService.get_selected_skin(session, user.id)

    text = (
        "🎨 **Change Skin / تغییر پوسته**\n\n"
        f"پوسته‌ی فعلی: **{profile_skin_label(selected_skin)}**\n\n"
        "یکی از چهار پوسته را انتخاب کن. این انتخاب فقط ظاهر پروفایل را عوض می‌کند "
        "و روی XP یا Level اثری ندارد."
    )
    keyboard = get_profile_skin_keyboard(selected_skin)

    await safe_answer_callback(callback)
    if callback.message:
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=keyboard
        )
    await _claim_panel(callback, "profile", prev_message_id)


@router.callback_query(F.data.startswith("profile_skin_select:"))
async def handle_profile_skin_selection(callback: CallbackQuery, bot: Bot):
    """Persist a selected skin and refresh the active choice in the picker."""
    user = callback.from_user
    if not user:
        return

    from sib.services.panel import (
        LOCKED_MESSAGE,
        PanelAuthorizationError,
        resolve_personal_actor,
    )
    from sib.services.profile_skin import (
        ProfileSkinService,
        profile_skin_label,
        require_profile_skin,
    )

    _prefix, _separator, requested_value = (callback.data or "").partition(":")
    try:
        requested_skin = require_profile_skin(requested_value)
    except ValueError:
        await safe_answer_callback(
            callback, "این پوسته در دسترس نیست.", show_alert=True
        )
        return

    try:
        actor_id, _message_id = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        selected_skin = await ProfileSkinService.set_selected_skin(
            session, actor_id, requested_skin
        )

    text = (
        "🎨 **Change Skin / تغییر پوسته**\n\n"
        f"پوسته‌ی فعلی: **{profile_skin_label(selected_skin)}**\n\n"
        "انتخابت ذخیره شد؛ XP و Level بدون تغییر باقی می‌مانند."
    )
    keyboard = get_profile_skin_keyboard(selected_skin)

    await safe_answer_callback(
        callback, f"{profile_skin_label(selected_skin)} فعال شد."
    )
    if callback.message:
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=keyboard
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
        return await render_banner(callback, bot, "main_menu", text, kb)


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


@router.message(F.text.regexp(r"^\s*سیبام\s*$"))
async def handle_sib_balance_word(message: Message, bot: Bot):
    """'سیبام' -> show the SENDER's apple balance, with the basket artwork.

    Uses the authoritative AppleWalletService balance (group + private).
    The artwork is the reusable static asset 'apple_basket'
    (assets/items/apple_basket.jpg, overridable via /setasset) - no dynamic
    numbers or user info in the image itself. Never falls through to the
    'سیب' claim action.
    """
    user = message.from_user
    if not user:
        return

    from sib.services.apple_wallet import AppleWalletService
    from sib.services.assets import AssetService
    from sib.services.character_art import photo_arg

    async with get_session() as session:
        from sib.services.user import UserService

        await UserService.get_or_create_user(session, user)
        balance = await AppleWalletService.get_balance(session, user.id)
        file_id, fs_path = await AssetService.resolve_photo(session, "apple_basket")

    text = f"🍏 موجودی سیب شما: {balance}"
    photo = photo_arg(file_id, fs_path)
    if photo is not None:
        try:
            await message.reply_photo(photo, caption=text, parse_mode="Markdown")
            return
        except Exception:  # pragma: no cover - presentation must not break
            pass
    # Graceful fallback: the balance text is the real content.
    await message.reply(text, parse_mode="Markdown")


async def _open_sender_profile(message: Message) -> None:
    """Open the SENDER's profile in the current chat (shared flow for the
    'سیب پروفایل' and 'سیب من' wake words - one profile system).

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
    from sib.services.player_xp import PlayerXPService
    from sib.services.profile_skin import ProfileSkinService, profile_skin_label

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        cards = await PlayerCardService.list_cards(session, user.id)
        main_card = await PlayerCardService.get_main_card(session, user.id)
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
        if main_card is not None:
            from sib.database.models import Character

            character = await session.get(Character, main_card.character_id)
            if character:
                fav_text = character.name_fa
                from sib.services.character_art import (
                    photo_arg,
                    resolve_character_photo,
                )

                file_id, fs_path = await resolve_character_photo(session, character)
                fav_photo = photo_arg(file_id, fs_path)

        # The ACTIVE spouse + shared Love, straight from the marriage domain.
        from sib.handlers.marriage import profile_marriage_lines

        marriage_lines = await profile_marriage_lines(session, user.id)
        account_progress = await PlayerXPService.get_progress(session, user.id)
        selected_skin = await ProfileSkinService.get_selected_skin(session, user.id)

    lines = [
        f"👤 **پروفایل سیبی {user.first_name or 'شما'}** 🍏",
        PlayerXPService.format_profile_progress(account_progress),
        f"🎨 **Change Skin / تغییر پوسته**: {profile_skin_label(selected_skin)}",
        "",
        f"🍎 سیب: `{apples}`",
        f"🌱 نهال: `{seedlings}`",
        f"🎴 تعداد کارت‌ها: `{len(cards)}`",
        fav_text.strip() if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    lines.extend(marriage_lines)
    profile_text = "\n".join(lines)
    profile_kb = get_profile_keyboard()

    # Green Apple delegates to its original Level-specific art resolver.
    from sib.services.profile_artwork import ProfileArtworkResolver

    main_photo = ProfileArtworkResolver.profile_photo(
        account_progress, skin=selected_skin, fallback_photo=fav_photo
    )
    sent = None
    if main_photo is not None:
        try:
            sent = await message.reply_photo(
                main_photo, caption=profile_text, reply_markup=profile_kb, parse_mode="Markdown"
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


@router.message(F.text.regexp(r"^\s*سیب\s+(پروفایل|من)\s*$"))
async def handle_sib_profile_word(message: Message, bot: Bot):
    """'سیب پروفایل' / 'سیب من' -> the PROFILE OF THE SENDER (spec #4).

    Both phrasings reuse the one profile flow. 'سیب من' is NOT a balance
    alias - 'سیبام' shows the apple balance.
    """
    await _open_sender_profile(message)


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
