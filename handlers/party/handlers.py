"""Party and game panel handlers."""

import asyncio

from aiogram import Router, F
from aiogram.filters import Command
from aiogram import Bot
from typing import Optional
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from sib.core.errors import (
    ActiveSessionExistsException,
    PartyFullException,
    PartyNotFoundException,
    InvalidActionException,
)
from sib.database.connection import get_session
from sib.database.models import Game, Party, PartyMember, PartyStatus
from sib.keyboards.party import (
    MODE_TITLES,
    get_game_selection_keyboard,
    get_games_menu_keyboard,
    get_game_mode_keyboard,
    get_match_type_keyboard,
    get_party_action_keyboard,
    get_active_parties_keyboard,
)
from sib.handlers.party.renderer import render_party_card
from sib.services.user import UserService
from sib.services.group import GroupService
from sib.services.party import PartyService
from sib.services.panel import PanelOwnershipService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="party")


async def build_games_menu(session, group_id: int) -> tuple:
    """(text, kb) for the games menu screen - the 'منوی بازی‌ها'."""
    active_parties = await PartyService.get_active_parties_for_group(session, group_id)
    games = list(
        (await session.scalars(select(Game).where(Game.is_active == True))).all()
    )
    text = "🍏 **منوی بازی‌های سیب**\nبازی مورد نظرتون رو برای شروع انتخاب کن:"
    kb = get_games_menu_keyboard(games, active_count=len(active_parties))
    return text, kb


async def build_active_games_menu(session, group_id: int) -> tuple:
    """(text, kb) for the active-games screen - 'بازی‌های فعال گروه'."""
    active_parties = await PartyService.get_active_parties_for_group(session, group_id)
    if active_parties:
        text = (
            "🍏 **بازی‌های فعال گروه:**\n"
            "همه‌ی بازی‌های زنده (در انتظار، کامل، در صف و در حال مسابقه) اینجاست؛ "
            "برای دیدن جزئیات انتخاب کن:"
        )
    else:
        text = (
            "🍏 **در حال حاضر هیچ بازی فعالی در این گروه وجود ندارد!**\n"
            "یک بازی جدید بساز تا تیم بسازیم:"
        )
    kb = get_active_parties_keyboard(active_parties)
    return text, kb


@router.message(Command("games"))
async def cmd_group_games(message: Message):
    """Show group games menu."""
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("🍏 این دستور فقط درون گروه‌های تلگرام قابل استفاده است!")
        return

    async with get_session() as session:
        # Register group and user
        if message.from_user:
            await UserService.get_or_create_user(session, message.from_user)
        await GroupService.get_or_create_group(session, message.chat)
        text, kb = await build_games_menu(session, message.chat.id)

        from sib.services.banner import send_screen_reply

        await send_screen_reply(message, "games", text, kb)


LEAVE_PARTY_TRIGGERS = {"ترک پارتی", "خروج از پارتی"}
NOT_IN_PARTY_TEXT = "شما در حال حاضر در هیچ پارتی‌ای نیستید."
LEFT_PARTY_TEXT = "✅ با موفقیت از پارتی خارج شدید."
LEADER_TRANSFERRED_TEXT = "شما لیدر بودید. لیدری به نفر بعدی منتقل شد و شما خارج شدید."
PARTY_DISBANDED_TEXT = "پارتی منحل شد."


@router.message(Command("leave_party"))
@router.message(F.text.in_({"ترک پارتی", "خروج از پارتی"}))
async def cmd_leave_party(message: Message):
    """Leave all active parties the user is currently a member of."""
    if not message.from_user:
        return

    async with get_session() as session:
        outcomes = await PartyService.leave_all_parties(
            session, message.from_user.id
        )

    if not outcomes:
        await message.reply(NOT_IN_PARTY_TEXT)
        return

    replies: list[str] = []
    for outcome in outcomes:
        if outcome["was_cancelled"]:
            reply_text = PARTY_DISBANDED_TEXT
        elif outcome["was_leader"]:
            reply_text = LEADER_TRANSFERRED_TEXT
        else:
            reply_text = LEFT_PARTY_TEXT
        if reply_text not in replies:
            replies.append(reply_text)

    await message.reply("\n".join(replies))


@router.callback_query(F.data == "group_games_panel")
async def handle_games_panel(callback: CallbackQuery, bot: Bot):
    """Screen: the games menu ('منوی بازی‌ها')."""
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await callback.answer("این بخش مخصوص گروه‌هاست 🍏", show_alert=True)
        return

    async with get_session() as session:
        # The games menu is a shared group panel: anyone may use it.
        message = callback.message
        if message is not None:
            chat_obj = getattr(message, "chat", None)
            msg_id = getattr(message, "message_id", None)
            if chat_obj is not None and msg_id:
                await PanelOwnershipService.release(session, chat_obj.id, msg_id)

        text, kb = await build_games_menu(session, chat.id)

        from sib.services.banner import render_banner

        await safe_answer_callback(callback)
        if callback.message:
            await render_banner(callback, bot, "games", text, kb)


@router.callback_query(F.data == "group_active_games")
async def handle_active_games(callback: CallbackQuery, bot: Bot):
    """Screen: active games of the group ('بازی‌های فعال گروه')."""
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await callback.answer("این بخش مخصوص گروه‌هاست 🍏", show_alert=True)
        return

    async with get_session() as session:
        message = callback.message
        if message is not None:
            chat_obj = getattr(message, "chat", None)
            msg_id = getattr(message, "message_id", None)
            if chat_obj is not None and msg_id:
                await PanelOwnershipService.release(session, chat_obj.id, msg_id)

        text, kb = await build_active_games_menu(session, chat.id)

        await safe_answer_callback(callback)
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "create_new_party_menu")
async def handle_create_new_party_menu(callback: CallbackQuery, bot: Bot):
    """Show game selection list to start a new party."""
    async with get_session() as session:
        games = list((await session.scalars(select(Game).where(Game.is_active == True))).all())
        text = "🎮 **یه بازی برای ساخت مسابقه انتخاب کن:**"
        await callback.answer()
        await safe_edit_callback_message(
            callback,
            bot,
            text,
            reply_markup=get_game_selection_keyboard(
                games, back_to="group_active_games", back_label="↩️ بازگشت به بازی‌های فعال"
            ),
        )


@router.callback_query(F.data.startswith("select_game:"))
async def handle_select_game(callback: CallbackQuery, bot: Bot):
    """Show modes (solo, 2v2, 4v4) for the selected game.

    Callback format: ``select_game:{code}`` or ``select_game:{code}:{back}``
    where ``{back}`` is the contextual parent screen (default: games menu).
    """
    parts = callback.data.split(":")
    game_code = parts[1]
    back_to = parts[2] if len(parts) > 2 and parts[2] else "group_games_panel"

    async with get_session() as session:
        game = await session.scalar(select(Game).where(Game.code == game_code))
        if not game:
            await callback.answer("بازی یافت نشد 🍏", show_alert=True)
            return

        text = (
            f"🎮 **{game.name_fa}**\n\n"
            f"📝 {game.description_fa}\n\n"
            f"حالت بازی مورد نظرت رو انتخاب کن:"
        )
        await callback.answer()
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=get_game_mode_keyboard(game, back_to=back_to)
        )


@router.callback_query(F.data.startswith("select_mode:"))
async def handle_select_mode(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Match type screen for one game mode (Game -> Mode -> Match Type).

    Callback format: ``select_mode:{code}:{mode}:{back}``. The mode itself is
    the SAME for both choices; only the match type changes.
    """
    parts = callback.data.split(":")
    game_code = parts[1]
    mode = parts[2] if len(parts) > 2 else "solo"
    back_to = parts[3] if len(parts) > 3 and parts[3] else "group_games_panel"

    async with get_session() as session:
        game = await session.scalar(select(Game).where(Game.code == game_code))
        if not game:
            await callback.answer("بازی یافت نشد 🍏", show_alert=True)
            return

        mode_text = MODE_TITLES.get(mode, mode)
        text = (
            f"🎮 **{game.name_fa}**\n"
            f"⚔️ **حالت:** {mode_text}\n\n"
            "می‌خوای رقابتی بازی کنی یا رفیقانه؟\n\n"
            "🔎 **پیدا کردن حریف:** مسابقهٔ رقابتی با حریف از گروه‌های دیگه "
            "(روی رتبهٔ گروه اثر داره).\n"
            "🎉 **بازی با اعضای گروه:** اعضای همین گروه دو تیم رو پر می‌کنن "
            "(با بات هم می‌تونی جای خالی رو پر کنی). نتیجه‌اش روی رتبه اثر نداره "
            "و به‌جای نهال، سیب جایزه می‌ده. 🍏"
        )
        await callback.answer()
        await safe_edit_callback_message(
            callback,
            bot,
            text,
            reply_markup=get_match_type_keyboard(game, mode, back_to=back_to),
        )


@router.callback_query(F.data.startswith("create_party:"))
async def handle_create_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Create a new party for the chosen game and mode.

    DB work (user lock + party creation) commits BEFORE any Telegram await:
    a card render with flood-retry sleeps must never extend the session that
    holds the user/party row locks the maintenance worker needs.
    """
    parts = callback.data.split(":")
    game_code = parts[1]
    mode = parts[2]

    user = callback.from_user
    chat = callback.message.chat if callback.message else None

    if not user or not chat:
        return

    error_message: Optional[str] = None
    text: Optional[str] = None
    kb = None
    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        await GroupService.get_or_create_group(session, chat)

        try:
            party = await PartyService.create_party(
                session=session,
                group_id=chat.id,
                creator_user_id=user.id,
                game_code=game_code,
                mode=mode,
            )
        except (ActiveSessionExistsException, InvalidActionException) as e:
            await session.rollback()
            error_message = e.message
            party = None
        if error_message is None:
            text = render_party_card(party)
            kb = get_party_action_keyboard(party, is_member=True, is_host=True)

    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    # The party card stays claimed to its CREATOR (cancel is a
    # creator-right). Joins/views are public and pass the shared
    # two-tier gate without any ownership (spec #6).
    await callback.answer("پارتی با موفقیت ایجاد شد! 🍏")
    if callback.message:
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("view_party:"))
async def handle_view_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """View details of a specific party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    async with get_session() as session:
        party = await PartyService.get_party_by_id(session, party_id)
        # LIVE states (including IN_QUEUE / IN_MATCH) are always viewable:
        # the Active Games screen lists every state that blocks a new game,
        # so pressing those rows must never dead-end.
        if not party or party.status == PartyStatus.CANCELLED:
            await callback.answer("این پارتی دیگر در دسترس نیست 🍏", show_alert=True)
            return

        is_member = any(m.user_id == user.id for m in party.members) if user else False
        is_host = bool(user) and party.creator_user_id == user.id
        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=is_member, is_host=is_host)

        await callback.answer()
        if callback.message:
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("join_party:"))
async def handle_join_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Join an open party slot (friendly parties may pick their team).

    Callback format: ``join_party:{id}`` or ``join_party:{id}:{team}``.
    """
    parts = callback.data.split(":")
    party_id = int(parts[1])
    team = None
    if len(parts) > 2 and parts[2]:
        try:
            team = int(parts[2])
        except ValueError:
            team = None
    user = callback.from_user

    if not user:
        return

    error_message: Optional[str] = None
    text: Optional[str] = None
    kb = None
    async with get_session() as session:
        await UserService.get_or_create_user(session, user)

        try:
            party = await PartyService.join_party(session, party_id, user.id, team=team)
        except (ActiveSessionExistsException, PartyFullException, PartyNotFoundException, InvalidActionException) as e:
            await session.rollback()
            error_message = e.message
            party = None
        if error_message is None:
            # Joining FOR UPDATE-locks the user and the party row; the card
            # is built here but rendered only after the transaction commits.
            text = render_party_card(party)
            kb = get_party_action_keyboard(
                party, is_member=True, is_host=party.creator_user_id == user.id
            )

    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("به پارتی اضافه شدی! 🍏✨")
    if callback.message:
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)



# How long the cancellation state stays visible before the same message
# is restored to the games menu (tests set this to 0).
PARTY_CANCEL_RESTORE_DELAY = 1.2


async def _cancel_and_restore_menu(
    callback: CallbackQuery, cancel_text: str, bot: Optional[Bot] = None
) -> None:
    """Party cancellation UX (solo / 2v2 / 4v4): show the cancellation
    state, then render the games menu screen back over it.

    Shared helper so every party mode behaves identically. The caller
    keeps its established cancellation wording. Authorization, queue and
    party cleanup happen BEFORE this (in the service) - this only shows
    the message after the cancellation is safely persisted.

    Both steps go through the central screen renderer: whatever the
    current message is (text or photo), the games menu ends up rendered
    as its own screen (banner + caption + keyboard) and the live message
    reference follows any replacement. Never a dead end.
    """
    message = callback.message
    if message is None:
        return
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)

    # 1) transient cancellation state (established wording preserved).
    try:
        await safe_edit_callback_message(callback, bot, cancel_text, reply_markup=None)
    except Exception:  # pragma: no cover - defensive
        pass

    if PARTY_CANCEL_RESTORE_DELAY > 0:
        await asyncio.sleep(PARTY_CANCEL_RESTORE_DELAY)

    # 2) restore the games menu screen - never a dead end.
    if not chat_id:
        return
    async with get_session() as session:
        text, kb = await build_games_menu(session, chat_id)
    try:
        from sib.services.banner import banner_photo

        await safe_edit_callback_message(
            callback, bot, text, reply_markup=kb, photo=banner_photo("games")
        )
    except Exception:  # pragma: no cover - defensive
        pass


@router.callback_query(F.data.startswith("leave_party:"))
async def handle_leave_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Leave a party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    error_message: Optional[str] = None
    was_cancelled = False
    text: Optional[str] = None
    kb = None
    async with get_session() as session:
        try:
            party, was_cancelled = await PartyService.leave_party(session, party_id, user.id)
        except (PartyNotFoundException, InvalidActionException) as e:
            await session.rollback()
            error_message = e.message
            party = None
        if error_message is None and not was_cancelled:
            text = render_party_card(party)
            kb = get_party_action_keyboard(party, is_member=False)

    # Leaving locks the party row; every confirmation / menu restore (which
    # includes its own delay + renders) waits for the commit first.
    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    if was_cancelled:
        await callback.answer("شما خارج شدید و چون کسی نمانده بود، پارتی لغو شد 🍏")
        await _cancel_and_restore_menu(callback, "❌ **این پارتی لغو شد.**", bot)
    else:
        await callback.answer("از پارتی خارج شدی 🍏")
        if callback.message:
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("cancel_party:"))
async def handle_cancel_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Cancel a party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    error_message: Optional[str] = None
    async with get_session() as session:
        try:
            await PartyService.cancel_party(session, party_id, user.id)
        except (PartyNotFoundException, InvalidActionException) as e:
            await session.rollback()
            error_message = e.message

    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("پارتی لغو شد 🍏")
    await _cancel_and_restore_menu(
        callback, "❌ **این پارتی توسط کاربر لغو گردید.**", bot
    )


@router.callback_query(F.data == "close_panel")
async def handle_close_panel(callback: CallbackQuery):
    """Close the panel message."""
    await callback.answer()
    if callback.message:
        await callback.message.delete()
