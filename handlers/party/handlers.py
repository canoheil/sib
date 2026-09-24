"""Party and game panel handlers."""

from aiogram import Router, F
from aiogram.filters import Command
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
    get_game_mode_keyboard,
    get_party_action_keyboard,
    get_active_parties_keyboard,
)
from sib.handlers.party.renderer import render_party_card
from sib.services.user import UserService
from sib.services.group import GroupService
from sib.services.party import PartyService

router = Router(name="party")


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

        active_parties = await PartyService.get_active_parties_for_group(session, message.chat.id)
        if active_parties:
            text = "🍏 **بازی‌های فعال و پارتی‌های باز گروه:**\nبرای پیوستن یا مشاهده جزئیات انتخاب کنید:"
            kb = get_active_parties_keyboard(active_parties)
        else:
            games = list((await session.scalars(select(Game).where(Game.is_active == True))).all())
            text = "🍏 **منوی بازی‌های سیب**\nبازی مورد نظرتون رو برای شروع انتخاب کنید:"
            kb = get_game_selection_keyboard(games)

        await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "group_games_panel")
async def handle_games_panel(callback: CallbackQuery):
    """Handle opening or refreshing the active parties/games panel."""
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await callback.answer("این بخش مخصوص گروه‌هاست 🍏", show_alert=True)
        return

    async with get_session() as session:
        active_parties = await PartyService.get_active_parties_for_group(session, chat.id)
        if active_parties:
            text = "🍏 **بازی‌های فعال و در حال انتظار گروه:**"
            kb = get_active_parties_keyboard(active_parties)
        else:
            games = list((await session.scalars(select(Game).where(Game.is_active == True))).all())
            text = "🍏 **در حال حاضر هیچ پارتی فعالی در این گروه وجود ندارد!**\nیک بازی انتخاب کن تا تیم بسازیم:"
            kb = get_game_selection_keyboard(games)

        await callback.answer()
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "create_new_party_menu")
async def handle_create_new_party_menu(callback: CallbackQuery):
    """Show game selection list to start a new party."""
    async with get_session() as session:
        games = list((await session.scalars(select(Game).where(Game.is_active == True))).all())
        text = "🎮 **یک بازی برای ایجاد مسابقه انتخاب کنید:**"
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(text, reply_markup=get_game_selection_keyboard(games), parse_mode="Markdown")


@router.callback_query(F.data.startswith("select_game:"))
async def handle_select_game(callback: CallbackQuery):
    """Show modes (solo, 2v2, 4v4) for the selected game."""
    game_code = callback.data.split(":")[1]

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
        if callback.message:
            await callback.message.edit_text(text, reply_markup=get_game_mode_keyboard(game), parse_mode="Markdown")


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
            if callback.message:
                await callback.message.edit_text("❌ **این پارتی لغو شد.**", reply_markup=None, parse_mode="Markdown")
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
        if callback.message:
            await callback.message.edit_text("❌ **این پارتی توسط کاربر لغو گردید.**", reply_markup=None, parse_mode="Markdown")


@router.callback_query(F.data == "close_panel")
async def handle_close_panel(callback: CallbackQuery):
    """Close the panel message."""
    await callback.answer()
    if callback.message:
        await callback.message.delete()
