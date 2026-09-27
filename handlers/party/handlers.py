"""Party and game panel handlers."""

import asyncio

from aiogram import Router, F
from aiogram.filters import Command
from aiogram import Bot
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
    get_game_selection_keyboard,
    get_games_menu_keyboard,
    get_game_mode_keyboard,
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
    text = "🍏 **منوی بازی‌های سیب**\nبازی مورد نظرتون رو برای شروع انتخاب کنید:"
    kb = get_games_menu_keyboard(games, active_count=len(active_parties))
    return text, kb


async def build_active_games_menu(session, group_id: int) -> tuple:
    """(text, kb) for the active-games screen - 'بازی‌های فعال گروه'."""
    active_parties = await PartyService.get_active_parties_for_group(session, group_id)
    if active_parties:
        text = "🍏 **بازی‌های فعال و در حال انتظار گروه:**\nبرای پیوستن یا مشاهده جزئیات انتخاب کنید:"
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

        await message.reply(text, reply_markup=kb, parse_mode="Markdown")


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

        await safe_answer_callback(callback)
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


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
        text = "🎮 **یک بازی برای ایجاد مسابقه انتخاب کنید:**"
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
            f"حالت بازی مورد نظرتان را انتخاب کنید:"
        )
        await callback.answer()
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=get_game_mode_keyboard(game, back_to=back_to)
        )


@router.callback_query(F.data.startswith("create_party:"))
async def handle_create_party(callback: CallbackQuery):
    """Create a new party for the chosen game and mode."""
    parts = callback.data.split(":")
    game_code = parts[1]
    mode = parts[2]

    user = callback.from_user
    chat = callback.message.chat if callback.message else None

    if not user or not chat:
        return

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
        except ActiveSessionExistsException as e:
            await callback.answer(e.message, show_alert=True)
            return
        except InvalidActionException as e:
            await callback.answer(e.message, show_alert=True)
            return

        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=True)

        # The party card stays claimed to its CREATOR (cancel is a
        # creator-right). Joins/views are public and pass the shared
        # two-tier gate without any ownership (spec #6).
        await callback.answer("پارتی با موفقیت ایجاد شد! 🍏")
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("view_party:"))
async def handle_view_party(callback: CallbackQuery):
    """View details of a specific party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    async with get_session() as session:
        party = await PartyService.get_party_by_id(session, party_id)
        if not party or party.status in (PartyStatus.CANCELLED, PartyStatus.IN_MATCH):
            await callback.answer("این پارتی دیگر در دسترس نیست 🍏", show_alert=True)
            return

        is_member = any(m.user_id == user.id for m in party.members) if user else False
        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=is_member)

        await callback.answer()
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("join_party:"))
async def handle_join_party(callback: CallbackQuery):
    """Join an open party slot."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)

        try:
            party = await PartyService.join_party(session, party_id, user.id)
        except (ActiveSessionExistsException, PartyFullException, PartyNotFoundException, InvalidActionException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=True)

        await callback.answer("به پارتی اضافه شدی! 🍏✨")
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")



# How long the cancellation state stays visible before the same message
# is restored to the games menu (tests set this to 0).
PARTY_CANCEL_RESTORE_DELAY = 1.2


async def _cancel_and_restore_menu(callback: CallbackQuery, cancel_text: str) -> None:
    """Party cancellation UX (solo / 2v2 / 4v4): show the cancellation
    state, then edit the SAME message back into the appropriate menu.

    Shared helper so every party mode behaves identically. The caller
    keeps its established cancellation wording. Authorization, queue and
    party cleanup happen BEFORE this (in the service) - this only edits
    the message after the cancellation is safely persisted.
    """
    message = callback.message
    if message is None:
        return
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)

    # 1) transient cancellation state (established wording preserved).
    try:
        await message.edit_text(cancel_text, reply_markup=None, parse_mode="Markdown")
    except Exception:  # pragma: no cover - defensive
        pass

    if PARTY_CANCEL_RESTORE_DELAY > 0:
        await asyncio.sleep(PARTY_CANCEL_RESTORE_DELAY)

    # 2) restore the games menu on the same message - never a dead end.
    if not chat_id:
        return
    async with get_session() as session:
        text, kb = await build_games_menu(session, chat_id)
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:  # pragma: no cover - defensive
        pass


@router.callback_query(F.data.startswith("leave_party:"))
async def handle_leave_party(callback: CallbackQuery):
    """Leave a party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            party, was_cancelled = await PartyService.leave_party(session, party_id, user.id)
        except (PartyNotFoundException, InvalidActionException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        if was_cancelled:
            await callback.answer("شما خارج شدید و چون کسی نمانده بود، پارتی لغو شد 🍏")
            await _cancel_and_restore_menu(callback, "❌ **این پارتی لغو شد.**")
        else:
            await callback.answer("از پارتی خارج شدی 🍏")
            text = render_party_card(party)
            kb = get_party_action_keyboard(party, is_member=False)
            if callback.message:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("cancel_party:"))
async def handle_cancel_party(callback: CallbackQuery):
    """Cancel a party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            await PartyService.cancel_party(session, party_id, user.id)
        except (PartyNotFoundException, InvalidActionException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("پارتی لغو شد 🍏")
        await _cancel_and_restore_menu(
            callback, "❌ **این پارتی توسط کاربر لغو گردید.**"
        )


@router.callback_query(F.data == "close_panel")
async def handle_close_panel(callback: CallbackQuery):
    """Close the panel message."""
    await callback.answer()
    if callback.message:
        await callback.message.delete()


@router.callback_query(F.data.startswith("play_bot:"))
async def handle_play_bot(callback: CallbackQuery, bot):
    """Start a solo match against the game bot."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    from sib.core.errors import PartyNotFoundException
    from sib.services.bot_match import BotMatchService
    from sib.games.registry import get_game_engine
    import json

    async with get_session() as session:
        try:
            match = await BotMatchService.create_bot_match(
                session=session,
                party_id=party_id,
                user_id=user.id,
            )
        except (
            PartyNotFoundException,
            ActiveSessionExistsException,
            InvalidActionException,
        ) as e:
            await callback.answer(e.message, show_alert=True)
            return

        game = await session.scalar(
            select(Game).where(Game.id == match.game_id)
        )

        if not game:
            await callback.answer("بازی یافت نشد 🍏", show_alert=True)
            return

        engine = get_game_engine(game.code)
        state = json.loads(match.game_state)

        # If the Bot starts, execute its turn before showing the board.
        from sib.bots.runner import run_bot_turns

        if match.is_bot_match and not engine.is_finished(state):
            state = await run_bot_turns(match, state)
            match.game_state = json.dumps(state, ensure_ascii=False)

        text = engine.render_for_group(
            state,
            match.group1_id,
            user.id,
        )
        keyboard = engine.get_keyboard(
            state,
            match.id,
            user.id,
        )

        if callback.message:
            await callback.message.edit_text(
                "🤖 **بازی با Bot شروع شد!**\n\n" + text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            match.group1_message_id = callback.message.message_id

        await callback.answer("🤖 بازی با Bot شروع شد!")
