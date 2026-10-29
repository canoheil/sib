"""Start command and basic navigation handlers."""

import re
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sib.core.config import settings
from sib.core.errors import MESSAGES
from sib.core.logging import logger, log_event, Events
from sib.core.text import escape_markdown, plain_display_name, safe_name
from sib.database.connection import get_session
from sib.keyboards.common import (
    get_private_start_keyboard,
    get_group_welcome_keyboard,
    get_cards_keyboard,
    get_profile_keyboard,
    get_profile_skin_keyboard,
    get_referral_invite_keyboard,
    nav_back_row,
)
from sib.services.nav import back_label_for, back_target, parse_callback, with_origin
from sib.services.help_content import (
    HELP_BACK_LABEL,
    HELP_SECTIONS,
    LEGACY_TOPIC_SECTIONS,
    SUPPORT_HANDLE,
    get_child,
    get_section,
    index_callback_data,
    parse_help_callback,
    render_index_text,
    render_section_text,
    section_callback_data,
)
from sib.services.panel import PanelOwnershipService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.membership.checker import MembershipService
from sib.services.onboarding import OnboardingService, StarterPack
from sib.services.referral import INVITEE_BONUS_TEXT, ReferralService
from sib.services.user import UserService
from sib.services.group import GroupService

router = Router(name="start")

# NOTE: SUPPORT_HANDLE (imported above from sib.services.help_content) is
# the official handle — it must appear in both /start messages (Phase A3)
# and in the guide's support section. One source of truth for both.


def _private_start_text(
    first_name: str,
    *,
    starter: Optional["StarterPack"] = None,
    gate: bool = False,
) -> str:
    """Private /start welcome: the FIRST-SESSION screen, not a sales pitch.

    One source of truth for both entry paths — the direct member welcome in
    ``handle_start`` and the post-verification welcome in
    ``handle_check_membership``.

    ``starter`` is the pack ``OnboardingService`` just paid (or None on a
    replay, when there is nothing new to announce): a brand-new player is told
    what they now own and what to press first, instead of being asked to add
    the bot to a group they may not administer. ``gate`` appends the optional
    official-channel line; it never replaces the menu.
    """
    if starter is not None and starter.granted:
        gift_lines = ["🎁 هدیهٔ شروع تو:"]
        if starter.card_name:
            gift_lines.append(
                f"• کارت «{starter.card_name}» — همین حالا کارت اصلی‌ات شد"
            )
        gift_lines.append(
            f"• {_fa_number(starter.apples)} سیب (موجودی فعلی: "
            f"{_fa_number(starter.balance)})"
        )
        gift = "\n".join(gift_lines) + "\n\n"
    else:
        gift = ""

    gate_line = (
        "📢 کانال رسمی سیب را دنبال کن تا از یورش‌ها و کارت‌های جدید باخبر شوی "
        "(دکمهٔ پایین).\n\n"
        if gate
        else ""
    )

    return (
        f"🍏 سلام {first_name} عزیز!\n\n"
        "سرزمین اواریا زیر یورش شیاسیب است و تو از امروز یکی از مدافعان آن هستی.\n\n"
        f"{gift}"
        "✨ همین حالا، داخل همین چت:\n"
        "۱. صندوق روزانه‌ات را باز کن 🎁\n"
        "۲. در باغت نهال بکار و برداشت کن 🌳\n"
        "۳. کارتت را با سیب ارتقا بده 🎴\n\n"
        "برای نبرد با بازیکن‌های دیگر و یورش شیاسیب، سیب را به گروهت اضافه کن "
        "(دکمهٔ پایین) — بیشتر بخش‌های بازی داخل گروه اتفاق می‌افتد.\n\n"
        "🎲 بازی‌های جانبی: کارت بالا، سنگ‌کاغذ‌قیچی، دوز، چهار در یک ردیف، کشتی جنگی.\n\n"
        f"{gate_line}"
        f"🔧 پشتیبانی: {SUPPORT_HANDLE}"
    )


def _fa_number(value: int) -> str:
    """Latin digits to Persian digits (bot copy is Persian-only)."""
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


async def _private_home(
    bot: Bot, user, starter: Optional["StarterPack"] = None
) -> tuple[str, InlineKeyboardMarkup]:
    """The private home screen: menu always, channel row only when missing.

    The membership gate is SOFT: a player who has not joined the official
    channel still gets the full first-session screen and the starter pack.
    Only the extra «📢 عضویت در کانال سیب» row is added — the old behaviour
    (one blocking "join the channel" message before any value) was the single
    biggest drop-off point of the funnel.
    """
    async with get_session() as session:
        is_member, missing_channel = await MembershipService.check_user_membership(
            bot, user.id, session
        )

    channel_link: Optional[str] = None
    if not is_member:
        channel_link = (
            missing_channel.invite_link
            if missing_channel is not None
            else settings.OFFICIAL_CHANNEL_LINK
        )

    text = _private_start_text(
        user.first_name or "دوست", starter=starter, gate=channel_link is not None
    )
    return text, get_private_start_keyboard(
        settings.BOT_USERNAME, gate_channel_link=channel_link
    )


def _start_payload(message: Message, command: Optional[CommandObject]) -> Optional[str]:
    """The ``/start`` deep-link payload (``command.args``).

    aiogram injects the parsed :class:`CommandObject`; the text fallback
    only covers direct unit-test calls that pass no command object.
    """
    args = getattr(command, "args", None) if command is not None else None
    if isinstance(args, str) and args.strip():
        return args
    text = getattr(message, "text", None)
    if isinstance(text, str):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[0].startswith("/start"):
            return parts[1]
    return None


@router.message(CommandStart(deep_link=True))
async def handle_start_deep_link(message: Message, bot: Bot, command: CommandObject):
    """``/start <payload>`` - e.g. a REF1 friend-referral deep link.

    ``CommandStart(deep_link=True)`` *only* matches when a payload is
    present, so the plain ``/start`` keeps flowing to ``handle_start``
    below; both entry points share the one implementation.
    """
    await handle_start(message, bot, command)


@router.message(CommandStart())
async def handle_start(message: Message, bot: Bot, command: Optional[CommandObject] = None):
    """Handle /start command in private and group chats.

    REF2: a ``ref_<inviter id>`` payload attributes a *brand-new* account
    to its inviter (validation in ``UserService``/``ReferralService`` -
    already-registered users and self-referrals are ignored) and the
    reward is paid IMMEDIATELY: the inviter gets 100 apples and the
    invitee 50 apples in the same transaction that created the account.
    No group activity is required any more - pressing ``/start`` in the
    private chat is enough. Paying is strictly idempotent, so a second
    ``/start`` (private or in a group) never pays twice.
    """
    user = message.from_user
    chat = message.chat

    if not user:
        return

    referrer_id = ReferralService.parse_referrer_id(_start_payload(message, command))
    referral_reward = None
    group_welcome_text = None

    async with get_session() as session:
        # Register user (a referral payload only counts for a NEW account)
        await UserService.get_or_create_user(
            session, user, referred_by_id=referrer_id
        )

        # REF2: the reward is paid RIGHT NOW - the invitee only has to
        # press /start in the private chat. Idempotent: a later /start
        # (private or group) pays nothing more.
        referral_reward = await ReferralService.grant_reward_if_due(
            session, user.id
        )

        # FIRST SESSION: the starter card + starter Apples, paid in this same
        # transaction, exactly once per account (the wallet ledger row is the
        # marker). Without it a new player owned nothing and the first card -
        # the object the whole game is about - was ~3 hours of grinding away.
        # Granted in groups too: a player whose first /start happens in a
        # group must also end up with a Main Card, which is what the invasion
        # power resolver counts.
        starter = await OnboardingService.grant_starter_pack(session, user.id)

        # 1. Group / Supergroup chat
        if chat.type in ("group", "supergroup"):
            group = await GroupService.get_or_create_group(session, chat)
            # Phase A3: Ovarya-first group welcome; keyboard unchanged.
            # Plain text (no parse mode) so group titles can never break
            # Markdown parsing.
            group_welcome_text = (
                f"🍏 سیب وارد شد! سلام به {chat.title}.\n\n"
                "گروه شما به جمع مدافعان اواریا پیوست. از این پس با هر یورش شیاسیب، "
                "در کنار هم می‌جنگید و با هر پیروزی، افتخاری به نام گروه‌تان ثبت می‌شود.\n\n"
                "برای شروع، دکمه‌های زیر را لمس کنید 👇\n\n"
                f"🔧 پشتیبانی: {SUPPORT_HANDLE}"
            )

    # The session is committed here, so the inviter is only ever notified
    # about apples that really landed in their wallet (REF2: the reward
    # is paid by this very /start, in private as well as in a group).
    await ReferralService.notify_inviter(bot, referral_reward)

    if group_welcome_text is not None:
        await message.reply(
            group_welcome_text,
            reply_markup=get_group_welcome_keyboard(),
            parse_mode=None,
        )
        return

    # 2. Private chat — the first-session screen.
    #
    # The starter pack was already paid inside the /start transaction above
    # (``starter``), so the welcome can promise exactly what landed. The
    # membership gate is soft now: the menu and the pack are never blocked by
    # a channel join, the join row is simply appended when it is missing.
    welcome_text, welcome_kb = await _private_home(bot, user, starter=starter)
    if referral_reward is not None:
        # REF2: the 50-apple referral bonus already landed in the
        # invitee's wallet - say it out loud, in Persian.
        welcome_text = f"{welcome_text}\n\n{INVITEE_BONUS_TEXT}"
    await message.answer(
        welcome_text,
        reply_markup=welcome_kb,
        parse_mode=None,
    )


@router.callback_query(F.data == "check_membership")
async def handle_check_membership(callback: CallbackQuery, bot: Bot):
    """Handle user clicking [✅ بررسی عضویت] on the soft channel row.

    Verifying membership never *unlocks* the game any more (the menu is always
    available); it only removes the «📢 عضویت در کانال سیب» row and confirms
    the join. A failed check keeps the row in place.
    """
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        is_member, missing_channel = await MembershipService.check_user_membership(bot, user.id, session)

    if not is_member:
        await callback.answer(MESSAGES["membership_check_failed"], show_alert=True)
        return

    await callback.answer(MESSAGES["membership_check_success"], show_alert=True)

    welcome_text, welcome_kb = await _private_home(bot, user)
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            welcome_text,
                reply_markup=get_private_start_keyboard(settings.BOT_USERNAME),
                parse_mode=None,
            )


# ---------------------------------------------------------------------------
# Phase A5 — the structured guide
# ---------------------------------------------------------------------------
# The guide is a TREE of sections (sib/services/help_content.py is the single
# source of truth): an index with one button per section, and every section
# is its own page with a single «🔙 بازگشت به راهنما» button. The pages are
# edited in place, never re-sent.


def _help_keyboard(origin: Optional[str] = None) -> InlineKeyboardMarkup:
    """The index keyboard: one button per section, then the way home."""
    from sib.keyboards.common import danger_button, primary_button

    buttons: list = []
    row: list = []
    for section in HELP_SECTIONS:
        row.append(
            primary_button(
                text=section.title_fa,
                callback_data=section_callback_data(section.section_key, origin),
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([danger_button(text="🏠 منوی اصلی", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _help_section_keyboard(origin: Optional[str] = None) -> InlineKeyboardMarkup:
    """A section page: exactly one button, back to the index."""
    from sib.keyboards.common import danger_button

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                danger_button(
                    text=HELP_BACK_LABEL,
                    callback_data=index_callback_data(origin),
                )
            ]
        ]
    )


def _help_menu_screen(origin: Optional[str] = None) -> tuple:
    """(text, keyboard) of the Guide index - ONE builder for every entry.

    Shared by the group/private menu button, the callback and the
    «سیب راهنما» text shortcut (Phase A4), so the guide index can never
    drift between entry points. ``origin`` only carries the contextual Back
    target; the shortcut opens it as a top-level screen (Back -> main menu).
    """
    return render_index_text(), _help_keyboard(origin)


@router.callback_query(F.data.startswith("help_menu"))
async def handle_help_menu(callback: CallbackQuery, bot: Bot):
    """The Guide index (spec #13 / Phase A5): the root of the section tree.

    The Guide is reachable from the group menu, the private menu and the
    «سیب راهنما» text shortcut; all of them render THIS index. The
    contextual origin is carried through the pages so Back returns to the
    screen that opened it. Back only navigates - it never touches an
    active match or party.
    """
    from sib.services.nav import parse_callback

    _base, origin = parse_callback(callback.data)
    text, keyboard = _help_menu_screen(origin)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback, bot, text, reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("help:"))
async def handle_help_navigation(callback: CallbackQuery, bot: Bot):
    """The Phase A5 tree: ``help:index`` and ``help:section:<key>``.

    Every page is edited IN PLACE (no new message, no spam) and the only
    button of a section page returns to the index.
    """
    target, origin = parse_help_callback(callback.data)

    if target == "index":
        text = render_index_text()
        keyboard = _help_keyboard(origin)
    elif target.startswith("section:"):
        parts = target.split(":")
        section = get_section(parts[1])
        if section is None:
            await safe_answer_callback(callback, "این بخش پیدا نشد 🍏", show_alert=True)
            return
        if len(parts) == 3:
            # A sub-page (depth 2 from the index).
            child = get_child(section, parts[2])
            if child is None:
                await safe_answer_callback(callback, "این بخش پیدا نشد 🍏", show_alert=True)
                return
            text = render_section_text(child)
        else:
            text = render_section_text(section)
        keyboard = _help_section_keyboard(origin)
    else:
        await safe_answer_callback(callback, "این بخش پیدا نشد 🍏", show_alert=True)
        return

    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("help_page:"))
async def handle_help_page(callback: CallbackQuery, bot: Bot):
    """LEGACY «help_page:<topic>» callbacks of the old guide.

    The old flat guide is gone, but its buttons may still live in old
    pinned messages; every legacy topic now opens the section that
    replaced it (LEGACY_TOPIC_SECTIONS).
    """
    from sib.services.nav import parse_callback

    # ``help_page:<topic>[:origin]`` — the topic is a HANDLER argument and
    # must never be eaten as the navigation origin (topics like
    # ``apple_battle`` share their name with a known screen callback).
    base, origin = parse_callback(callback.data, arg_segments=1)
    parts = base.split(":")
    topic = parts[1] if len(parts) > 1 else None
    section = get_section(LEGACY_TOPIC_SECTIONS.get(topic or ""))
    if section is None:
        await safe_answer_callback(callback, "این بخش پیدا نشد 🍏", show_alert=True)
        return
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        render_section_text(section),
        reply_markup=_help_section_keyboard(origin),
    )


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


async def _cards_screen(
    user_id: int, session, page: int = 0, origin: Optional[str] = None
) -> tuple:
    """(text, keyboard) of the «کارت‌های من» panel - ONE builder.

    Shared by the profile/main-menu button (``apple_cards``), its
    pagination and the «سیب کارت» text shortcut (Phase A4): the panel the
    shortcut opens is byte-for-byte the panel the button opens.
    """
    from sqlalchemy import select

    from sib.database.models import Character
    from sib.services.player_cards import PlayerCardService
    from sib.services.wiki import CollectionService

    text, total_pages = await CollectionService.render_collection(
        session, user_id, page=page
    )
    cards = await PlayerCardService.list_cards(session, user_id)
    main_card = await PlayerCardService.get_main_card(session, user_id)
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
    return text, kb


@router.callback_query(F.data.startswith("apple_cards"))
async def handle_apple_cards(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    _base, origin = parse_callback(callback.data)

    # Optional page argument: apple_cards[:page[:origin]]
    parts = _base.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        text, kb = await _cards_screen(user.id, session, page=page, origin=origin)

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
        from sib.services.friendship import FriendshipService

        friend_count = await FriendshipService.count_friends(session, user.id)
        account_progress = await PlayerXPService.get_progress(session, user.id)
        selected_skin = await ProfileSkinService.get_selected_skin(session, user.id)
        # The reminder switch shows the player's CURRENT preference; the
        # service reads the state row (absent row == reminders on).
        from sib.services.reminders import ReminderService

        reminders_on = not await ReminderService.is_opted_out(session, user.id)
        from sib.services.disease_service import get_status as get_disease_status

        disease_status = await get_disease_status(session, user.id)

    lines = [
        f"👤 **پروفایل سیبی {safe_name(user.first_name, 'شما')}** 🍏",
        PlayerXPService.format_profile_progress(account_progress),
        f"🎨 **Change Skin / تغییر پوسته**: {profile_skin_label(selected_skin)}",
        "",
        f"🍎 سیب: `{apples}`",
        f"🌱 نهال: `{seedlings}`",
        f"🎴 تعداد کارت‌ها: `{len(cards)}`",
        escape_markdown(fav_text.strip()) if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    lines.extend(marriage_lines)
    from sib.handlers.disease import disease_profile_lines

    lines.extend(disease_profile_lines(disease_status))
    from sib.services.friendship import profile_friend_count_line

    lines.append(profile_friend_count_line(friend_count))

    kb = get_profile_keyboard(reminders_on=reminders_on)

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


async def _skin_screen(user_id: int, session, telegram_user=None) -> tuple:
    """(text, keyboard) of the «تغییر پوسته» picker - ONE builder.

    Shared by the profile button (``profile_skin``) and the «سیب اسکین»
    text shortcut (Phase A4). ``telegram_user`` keeps the existing
    account-repair behaviour of the callback entry: the selector can also
    register an account first seen through a callback, while wake-word
    entries pass the sender.
    """
    from sib.services.profile_skin import ProfileSkinService, profile_skin_label

    if telegram_user is not None:
        await UserService.get_or_create_user(session, telegram_user)
    selected_skin = await ProfileSkinService.get_selected_skin(session, user_id)

    text = (
        "🎨 **Change Skin / تغییر پوسته**\n\n"
        f"پوسته‌ی فعلی: **{profile_skin_label(selected_skin)}**\n\n"
        "یکی از چهار پوسته را انتخاب کن. این انتخاب فقط ظاهر پروفایل را عوض می‌کند "
        "و روی XP یا Level اثری ندارد."
    )
    return text, get_profile_skin_keyboard(selected_skin)


@router.callback_query(F.data == "profile_skin")
async def handle_profile_skin(callback: CallbackQuery, bot: Bot):
    """Open the Change Skin choices for the profile owner."""
    user = callback.from_user
    if not user:
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        text, keyboard = await _skin_screen(user.id, session, telegram_user=user)

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


# ---------------------------------------------------------------------------
# REF1 — «🎁 دعوت دوستان» (profile -> invite friends)
# ---------------------------------------------------------------------------


def _referral_invite_screen(user_id: int) -> tuple:
    """(text, keyboard) of the invite-friends panel - ONE builder.

    The raw deep link is always in the text (long-press to copy, like
    everywhere else in Telegram); the button opens the share sheet.
    Plain text only: the link contains ``_`` (``ref_<id>``), which legacy
    Markdown would try to parse as italics.

    REF2: the reward is paid as soon as the friend presses ``/start`` -
    no group activity is required any more, so the copy says exactly that.
    """
    link = ReferralService.build_referral_link(settings.BOT_USERNAME, user_id)
    text = (
        "👋 دوستانت رو به سیب دعوت کن!\n\n"
        "با هر دوستی که با لینک تو وارد بازی بشه، "
        "۱۰۰ سیب هدیه می‌گیری و دوستت هم ۵۰ سیب هدیه می‌گیره.\n\n"
        "لینک اختصاصی شما:\n"
        f"{link}"
    )
    return text, get_referral_invite_keyboard(settings.BOT_USERNAME, user_id)


@router.callback_query(F.data == "referral_invite")
async def handle_referral_invite(callback: CallbackQuery, bot: Bot):
    """Show the player's personal referral deep link (profile button)."""
    user = callback.from_user
    if not user:
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        # Same account-repair behaviour as the skin picker: a player first
        # seen through this callback is registered before getting a link.
        await UserService.get_or_create_user(session, user)

    text, keyboard = _referral_invite_screen(user.id)
    await safe_answer_callback(callback)
    if callback.message:
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=keyboard, parse_mode=None
        )
    await _claim_panel(callback, "profile", prev_message_id)


def _main_menu_text_and_kb(user, chat) -> tuple:
    """Shared main-menu builder (context-aware): groups get the group menu,
    private chats get the REAL private menu. Used by both the 'main_menu'
    callback and the 'سیب منو' wake word - one source of truth.

    The private branch is the SAME keyboard /start renders
    (``get_private_start_keyboard``) minus the optional channel-gate row: a
    navigation button must not spend a chat-member API call, and the host of
    back buttons in private screens (guide, shop, garden, cards, wiki, chest)
    all land here, so this function decides what «منوی اصلی» means. It used to
    return the retired ``get_private_welcome_keyboard``, which is why the guide
    dumped players into an old menu that nothing else in the bot opens.
    """
    is_group = bool(chat and chat.type in ("group", "supergroup"))
    if is_group:
        text = "🍏 **منوی اصلی سیب**\nاز منوی زیر بخش مورد نظرت رو انتخاب کن 👇"
        kb = get_group_welcome_keyboard()
    else:
        name = safe_name(user.first_name, "دوست") if user else "دوست"
        text = (
            f"🍏 **منوی اصلی سیب**\n\n"
            f"سلام {name}! از منوی زیر بخش مورد نظرت رو انتخاب کن 👇"
        )
        kb = get_private_start_keyboard(settings.BOT_USERNAME)
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


# The three rare (inventory) apples shown under the normal balance.
# Codes are the canonical ``user_items`` keys.
SPECIAL_APPLE_LINES = (
    ("apple_gold", "🌕", "سیب طلایی"),
    ("apple_diamond", "💎", "سیب الماسی"),
    ("apple_obsidian", "🌑", "سیب ابسیدینی"),
)


def format_apple_balance_caption(
    balance: int, special_counts: dict[str, int] | None = None
) -> str:
    """The 'سیبام' caption: normal balance + the three special apples.

    Pure (no I/O) so it stays trivially testable. Every number is rendered
    with Persian digits. Special apples are real inventory items
    (``user_items``); normal apples are the wallet currency.
    """
    from sib.services.sib_claim import fa_digits

    counts = special_counts or {}
    lines = [f"🍏 موجودی سیب شما: {fa_digits(balance)}", ""]
    lines.extend(
        f"{emoji} {label}: {fa_digits(int(counts.get(key) or 0))}"
        for key, emoji, label in SPECIAL_APPLE_LINES
    )
    return "\n".join(lines)


@router.message(F.text.regexp(r"^\s*سیبام\s*$"))
async def handle_sib_balance_word(message: Message, bot: Bot):
    """'سیبام' -> show the SENDER's apple balance, with the basket artwork.

    Uses the authoritative AppleWalletService balance (group + private) plus
    the special-apple inventory counts (Golden / Diamond / Obsidian) from
    ``ItemService``. The artwork is the reusable static asset 'apple_basket'
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
    from sib.services.items import ItemService

    async with get_session() as session:
        from sib.services.user import UserService

        await UserService.get_or_create_user(session, user)
        balance = await AppleWalletService.get_balance(session, user.id)
        special_counts = {
            key: await ItemService.get_total(session, user.id, key)
            for key, _emoji, _label in SPECIAL_APPLE_LINES
        }
        file_id, fs_path = await AssetService.resolve_photo(session, "apple_basket")

    text = format_apple_balance_caption(balance, special_counts)
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
        from sib.services.disease_service import get_status as get_disease_status

        disease_status = await get_disease_status(session, user.id)

    lines = [
        f"👤 **پروفایل سیبی {safe_name(user.first_name, 'شما')}** 🍏",
        PlayerXPService.format_profile_progress(account_progress),
        f"🎨 **Change Skin / تغییر پوسته**: {profile_skin_label(selected_skin)}",
        "",
        f"🍎 سیب: `{apples}`",
        f"🌱 نهال: `{seedlings}`",
        f"🎴 تعداد کارت‌ها: `{len(cards)}`",
        escape_markdown(fav_text.strip()) if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    lines.extend(marriage_lines)
    from sib.handlers.disease import disease_profile_lines

    lines.extend(disease_profile_lines(disease_status))
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


# ---------------------------------------------------------------------------
# «پروفایلش» — view ANOTHER player's profile by replying to their message
# ---------------------------------------------------------------------------

PROFILE_VIEW_REPLY_REQUIRED_TEXT = (
    "برای دیدن پروفایل یک نفر، روی پیامش ریپلای کن و بنویس «پروفایلش»."
)
PROFILE_VIEW_TARGET_IS_BOT_TEXT = "ربات‌ها که پروفایل ندارن! 😄"
PROFILE_VIEW_NOT_REGISTERED_TEXT = "این کاربر هنوز توی بازی ثبت‌نام نکرده."
PROFILE_VIEW_NOT_A_USER_TEXT = (
    "این پیام مال یه بازیکن واقعی نیست؛ روی پیام یه بازیکن ریپلای کن."
)

PROFILE_VIEW_COMMAND = "پروفایلش"

# Typed-Persian folding: Arabic Yeh/Kaf -> Persian, and the invisible
# ZWNJ/bidi marks keyboards put inside Persian words.
_PROFILE_VIEW_FOLD = str.maketrans(
    {"ي": "ی", "ك": "ک", "\u200c": "", "\u200e": "", "\u200f": ""}
)


def is_profile_view_command(text: Optional[str]) -> bool:
    """True only for the exact word «پروفایلش» (case and spacing ignored).

    Anchored to the whole message, so «سیب پروفایل» and sentences that
    merely contain the word never trigger it.
    """
    normalized = " ".join((text or "").translate(_PROFILE_VIEW_FOLD).split())
    return normalized.casefold() == PROFILE_VIEW_COMMAND


class ProfileViewCommand(Filter):
    async def __call__(self, message: Message) -> bool:
        return is_profile_view_command(message.text)


async def _public_profile(session, target) -> tuple:
    """(text, photo) for ANOTHER player's profile - the privacy-safe view.

    Shows only in-game stats and the sanitized first name. It never reads
    or prints the Telegram ID, username or any ``tg://`` link, and it omits
    the private wallet (apples, seedlings) and the skin-change control,
    which belong to the owner. The spouse appears as a first name only,
    via ``profile_marriage_lines``.
    """
    from sqlalchemy import func, select as sa_select

    from sib.database.models import AppleBattle, AppleBattleStatus, Character
    from sib.handlers.marriage import profile_marriage_lines
    from sib.services.character_art import photo_arg, resolve_character_photo
    from sib.services.player_cards import PlayerCardService
    from sib.services.player_xp import PlayerXPService
    from sib.services.profile_artwork import ProfileArtworkResolver
    from sib.services.profile_skin import ProfileSkinService

    user_id = int(target.id)
    cards = await PlayerCardService.list_cards(session, user_id)
    main_card = await PlayerCardService.get_main_card(session, user_id)
    wins = await session.scalar(
        sa_select(func.count(AppleBattle.id))
        .where(AppleBattle.winner_id == user_id)
        .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
    )
    battles = await session.scalar(
        sa_select(func.count(AppleBattle.id)).where(
            ((AppleBattle.player1_id == user_id) | (AppleBattle.player2_id == user_id))
            & (AppleBattle.status == AppleBattleStatus.COMPLETED)
        )
    )

    fav_text = None
    fav_photo = None
    if main_card is not None:
        character = await session.get(Character, main_card.character_id)
        if character:
            fav_text = character.name_fa
            file_id, fs_path = await resolve_character_photo(session, character)
            fav_photo = photo_arg(file_id, fs_path)

    marriage_lines = await profile_marriage_lines(session, user_id)
    progress = await PlayerXPService.get_progress(session, user_id)
    skin = await ProfileSkinService.get_selected_skin(session, user_id)

    name = plain_display_name(target.first_name, "بازیکن")
    fav_line = (
        f"⭐ کارت اصلی: {escape_markdown(fav_text.strip())}"
        if fav_text
        else "⭐ کارت اصلی: انتخاب نشده"
    )
    lines = [
        f"👤 **پروفایل سیبی {name}** 🍏",
        PlayerXPService.format_profile_progress(progress),
        "",
        f"🎴 تعداد کارت‌ها: `{len(cards)}`",
        fav_line,
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    lines.extend(marriage_lines)
    photo = ProfileArtworkResolver.profile_photo(
        progress, skin=skin, fallback_photo=fav_photo
    )
    return "\n".join(lines), photo


@router.message(ProfileViewCommand())
async def handle_reply_profile_view(message: Message, bot: Bot):
    """«پروفایلش» as a REPLY -> send the replied-to player's profile.

    Validation runs before any write. Only an already-registered player has
    a profile; the target is never auto-registered. The result is a NEW
    message with no panel keyboard, because the owner-only controls
    (skin change, cards, etc.) do not apply to someone else's profile.
    """
    viewer = message.from_user
    if viewer is None:
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(PROFILE_VIEW_REPLY_REQUIRED_TEXT)
        return

    target = getattr(reply, "from_user", None)
    if target is None:
        # Channel posts and anonymous admins have no player identity.
        await message.reply(PROFILE_VIEW_NOT_A_USER_TEXT)
        return
    if getattr(target, "is_bot", False):
        await message.reply(PROFILE_VIEW_TARGET_IS_BOT_TEXT)
        return

    async with get_session() as session:
        await UserService.get_or_create_user(session, viewer)
        from sib.database.models import User

        registered = await session.get(User, int(target.id))
        if registered is None or registered.is_bot:
            rendered = None
        else:
            rendered = await _public_profile(session, registered)

    if rendered is None:
        await message.reply(PROFILE_VIEW_NOT_REGISTERED_TEXT)
        return

    text, photo = rendered
    sent = None
    if photo is not None:
        try:
            sent = await message.reply_photo(photo, caption=text, parse_mode="Markdown")
        except Exception:
            sent = None
    if sent is None:
        await message.reply(text, parse_mode="Markdown")


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


# ---------------------------------------------------------------------------
# Phase A4 — text shortcut aliases for existing menus
# ---------------------------------------------------------------------------
# Persian Yeh (U+06CC) / Arabic Yeh (U+064A) and Persian Keheh (U+06A9) /
# Arabic Kaf (U+0643) are interchangeable in typed Persian - the same
# convention the chest, marriage, hug and kiss wake words already use
# (handlers/daily_chest.py, handlers/marriage.py).
_YEH = r"[\u06cc\u064a]"
_KEH = r"[\u06a9\u0643]"

#: The accepted «سیب <target>» spellings, folded to their canonical target.
#: «لیدربورد» is the common typed spelling of the leaderboard shortcut.
SIB_SHORTCUT_TARGETS = {
    "شاپ": "shop",
    "کارت": "cards",
    "اسکین": "skin",
    "لیدربرد": "leaderboard",
    "لیدربورد": "leaderboard",
    "راهنما": "help",
}

#: ONE anchored pattern for all five aliases: the wake word «سیب» plus
#: EXACTLY one known target word (surrounding whitespace ignored). It can
#: never match a slash command, a sentence that merely contains «سیب شاپ»
#: or an unfinished phrase - ordinary conversation stays untouched.
SIB_SHORTCUT_PATTERN = (
    r"^\s*س" + _YEH + r"ب\s+(?:"
    r"شاپ"                                   # سیب شاپ     -> shop (/shop)
    r"|" + _KEH + r"ارت"                     # سیب کارت    -> My Cards
    r"|اس" + _KEH + _YEH + r"ن"              # سیب اسکین   -> Change Skin
    r"|ل" + _YEH + r"در(?:برد|بورد)"          # سیب لیدربرد -> leaderboard
    r"|راهنما"                               # سیب راهنما  -> Guide index
    r")\s*$"
)

# Characters folded before the target lookup: Arabic Yeh/Kaf variants and
# the invisible ZWNJ/bidi marks that keyboards insert inside Persian words.
_SHORTCUT_FOLD = str.maketrans(
    {"ي": "ی", "ك": "ک", "\u200c": "", "\u200e": "", "\u200f": ""}
)


def sib_shortcut_target(text: Optional[str]) -> Optional[str]:
    """Canonical target of an EXACT «سیب <target>» shortcut, else ``None``.

    The whole message must be the wake word plus ONE known target word:
    «سلام سیب شاپ», «سیب شاپ کن», a slash command and any ordinary
    sentence all return ``None`` and remain normal conversation.
    """
    normalized = (text or "").strip().translate(_SHORTCUT_FOLD)
    if normalized.startswith("/"):
        return None
    parts = normalized.split()
    if len(parts) != 2 or parts[0] != "سیب":
        return None
    return SIB_SHORTCUT_TARGETS.get(parts[1])


#: Exact menu phrases, mapped to the existing entry paths. Paired spellings
#: such as the Wiki and weekly-league aliases share one destination.
MENU_ALIAS_TARGETS = {
    "ویکی سیبی": "wiki",
    "سیب ویکی": "wiki",
    "باغ سیبی": "garden",
    "پارتی سیبی": "party",
    "کارت سیبی": "cards",
    "شاپ سیبی": "shop",
    "لیدربرد سیبی": "leaderboard",
    "صندوق سیبی": "daily_chest",
    "سیب لیگ": "league",
    "لیگ سیبی": "league",
}

# The regex remains anchored to the whole message like the existing
# shortcuts. It admits the same Arabic/Persian Yeh and Kaf variants as the
# normalizer, plus ZWNJ and bidi marks that the normalizer discards.
_MENU_ALIAS_VARIANT_CHARS = {
    "ی": r"[\u06cc\u064a]",
    "ک": r"[\u06a9\u0643]",
}
_MENU_ALIAS_IGNORABLE_MARKS = r"[\u200c\u200e\u200f]*"


def _menu_alias_phrase_pattern(phrase: str) -> str:
    """Regex for one canonical phrase with typed-Persian character folding."""
    return r"\s+".join(
        "".join(
            _MENU_ALIAS_IGNORABLE_MARKS
            + _MENU_ALIAS_VARIANT_CHARS.get(char, re.escape(char))
            + _MENU_ALIAS_IGNORABLE_MARKS
            for char in word
        )
        for word in phrase.split()
    )


MENU_ALIAS_PATTERN = (
    r"^[\s\u200c\u200e\u200f]*(?:"
    + "|".join(_menu_alias_phrase_pattern(phrase) for phrase in MENU_ALIAS_TARGETS)
    + r")[\s\u200c\u200e\u200f]*$"
)


def menu_alias_target(text: Optional[str]) -> Optional[str]:
    """Return the destination of an exact new menu alias, or ``None``.

    Arabic Yeh/Kaf, ZWNJ and bidi marks are folded exactly like the A4
    shortcut helper. Whitespace is normalized, but extra words never match.
    """
    normalized = " ".join((text or "").translate(_SHORTCUT_FOLD).split())
    if normalized.startswith("/"):
        return None
    return MENU_ALIAS_TARGETS.get(normalized)


async def _open_sender_cards(message: Message, bot: Bot) -> None:
    """«سیب کارت» -> the SENDER's «کارت‌های من» panel (profile button).

    The sender is always the subject and the replied panel is claimed to
    them, so inside a group only they can press its card buttons - exactly
    like the same button inside the profile.
    """
    user = message.from_user
    if not user:
        return

    from sib.services.banner import send_screen_reply

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        text, kb = await _cards_screen(user.id, session)
        sent = await send_screen_reply(message, "cards", text, kb)
        if sent is not None and sent.message_id:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="cards",
            )


async def _open_sender_skin(message: Message, bot: Bot) -> None:
    """«سیب اسکین» -> the SENDER's «تغییر پوسته» picker (profile button).

    Same screen and same owner lock as the profile entry: the picker is
    claimed to the sender, so skin changes stay personal in groups.
    """
    user = message.from_user
    if not user:
        return

    from sib.services.banner import send_screen_reply

    async with get_session() as session:
        text, kb = await _skin_screen(user.id, session, telegram_user=user)
        sent = await send_screen_reply(message, "skin", text, kb)
        if sent is not None and sent.message_id:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="profile",
            )


async def _open_help_menu(message: Message, bot: Bot) -> None:
    """«سیب راهنما» -> the same Guide index the «راهنما» button opens.

    The guide is a public screen (no owner lock, no banner), so it renders
    through the same builder and the same reply path as the wake words.
    """
    from sib.services.banner import send_screen_reply

    text, kb = _help_menu_screen()
    await send_screen_reply(message, "help", text, kb)


@router.message(Command("help"))
async def handle_help_command(message: Message, bot: Bot):
    """Open the existing Persian Guide index for Telegram's ``/help`` command."""
    await _open_help_menu(message, bot)


async def _open_shop_entry(message: Message, bot: Bot) -> None:
    """«سیب شاپ» -> the /shop command (same entry function, same claim)."""
    from sib.handlers.shop import cmd_shop

    await cmd_shop(message, bot)


async def _open_leaderboard_entry(message: Message, bot: Bot) -> None:
    """«سیب لیدربرد» -> the /leaderboard command (same entry function)."""
    from sib.handlers.leaderboard import cmd_leaderboard

    await cmd_leaderboard(message)


#: Shortcut target -> the function that opens it. Every opener delegates to
#: the SAME code path as the corresponding command/button; no screen is
#: rebuilt here.
_SIB_SHORTCUT_OPENERS = {
    "shop": _open_shop_entry,
    "cards": _open_sender_cards,
    "skin": _open_sender_skin,
    "leaderboard": _open_leaderboard_entry,
    "help": _open_help_menu,
}


@router.message(F.text.regexp(SIB_SHORTCUT_PATTERN))
async def handle_sib_shortcut_word(message: Message, bot: Bot):
    """Phase A4 aliases: «سیب شاپ»، «سیب کارت»، «سیب اسکین»، «سیب لیدربرد»، «سیب راهنما».

    ONE handler, ONE anchored pattern and ONE target map - the same
    registration mechanism as the other wake words in this router, so all
    five work identically in private chats and in groups: the sender is
    the subject and personal screens keep their existing owner lock.
    Slash commands and sentences never reach this handler.
    """
    target = sib_shortcut_target(message.text)
    if target is None:
        return
    opener = _SIB_SHORTCUT_OPENERS.get(target)
    if opener is None:  # pragma: no cover - map and pattern stay in sync
        return
    await opener(message, bot)


async def _open_wiki_menu_alias(message: Message, bot: Bot) -> None:
    from sib.handlers.wiki import cmd_wiki

    await cmd_wiki(message, bot)


async def _open_garden_menu_alias(message: Message, bot: Bot) -> None:
    await _open_sender_garden(message)


async def _open_party_menu_alias(message: Message, bot: Bot) -> None:
    from sib.handlers.party.handlers import cmd_group_games

    await cmd_group_games(message)


async def _open_cards_menu_alias(message: Message, bot: Bot) -> None:
    await _open_sender_cards(message, bot)


async def _open_shop_menu_alias(message: Message, bot: Bot) -> None:
    await _open_shop_entry(message, bot)


async def _open_leaderboard_menu_alias(message: Message, bot: Bot) -> None:
    await _open_leaderboard_entry(message, bot)


async def _open_league_menu_alias(message: Message, bot: Bot) -> None:
    from sib.handlers.league import cmd_league

    await cmd_league(message, bot)


async def _open_daily_chest_menu_alias(message: Message, bot: Bot) -> None:
    from sib.handlers.daily_chest import handle_chest_shortcut

    await handle_chest_shortcut(message, bot)


#: Every alias reuses the pre-existing entry point for its destination.
MENU_ALIAS_OPENERS = {
    "wiki": _open_wiki_menu_alias,
    "garden": _open_garden_menu_alias,
    "party": _open_party_menu_alias,
    "cards": _open_cards_menu_alias,
    "shop": _open_shop_menu_alias,
    "leaderboard": _open_leaderboard_menu_alias,
    "daily_chest": _open_daily_chest_menu_alias,
    "league": _open_league_menu_alias,
}


@router.message(F.text.regexp(MENU_ALIAS_PATTERN))
async def handle_menu_alias_word(message: Message, bot: Bot):
    """Open the existing menu for one of the newly added exact aliases."""
    target = menu_alias_target(message.text)
    if target is None:
        return
    opener = MENU_ALIAS_OPENERS.get(target)
    if opener is None:  # pragma: no cover - alias map and opener table stay in sync
        return
    await opener(message, bot)


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
