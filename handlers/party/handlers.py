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
from sib.database.models import Game, Match, MatchType, Party, PartyMember, PartyStatus
from sib.keyboards.party import (
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

PARTY_SELECTION_BACK_TARGETS = {"group_games_panel", "group_active_games"}


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
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "ساخت پارتی فقط از داخل گروه ممکنه 🍏", show_alert=True)
        return
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
    parts = (callback.data or "").split(":")
    if len(parts) < 2 or not parts[1]:
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "ساخت پارتی فقط از داخل گروه ممکنه 🍏", show_alert=True)
        return
    game_code = parts[1]
    back_to = parts[2] if len(parts) > 2 and parts[2] else "group_games_panel"
    if back_to not in PARTY_SELECTION_BACK_TARGETS:
        back_to = "group_games_panel"

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


@router.callback_query(F.data.startswith("select_match_type:"))
async def handle_select_match_type(callback: CallbackQuery, bot: Bot):
    """Show Competitive/Friendly choices after game and mode selection."""
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    game_code, mode = parts[1], parts[2]
    back_to = parts[3] if len(parts) > 3 and parts[3] else "group_games_panel"
    if back_to not in PARTY_SELECTION_BACK_TARGETS:
        back_to = "group_games_panel"
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "ساخت پارتی فقط از داخل گروه ممکنه 🍏", show_alert=True)
        return

    async with get_session() as session:
        game = await session.scalar(select(Game).where(Game.code == game_code))
        if not game:
            await safe_answer_callback(callback, "بازی یافت نشد 🍏", show_alert=True)
            return
        supported = {
            "solo": game.supports_solo,
            "2v2": game.supports_2v2,
            "4v4": game.supports_4v4,
        }.get(mode, False)
        if not supported:
            await safe_answer_callback(callback, "این حالت برای بازی انتخاب‌شده پشتیبانی نمی‌شه 🍏", show_alert=True)
            return

    text = (
        f"🎮 **{game.name_fa} — {mode}**\n\n"
        "نوع مسابقه رو انتخاب کن:\n"
        "🏆 رقابتی: بازی با گروه‌های دیگه و پیشرفت رقابتی.\n"
        "🤝 دوستانه: دو تیم از همین گروه؛ بدون نهال یا پیشرفت رقابتی."
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        text,
        reply_markup=get_match_type_keyboard(game, mode, back_to=back_to),
    )


@router.callback_query(F.data.startswith("create_party:"))
async def handle_create_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Create a Competitive or Friendly party in the current Telegram group."""
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    game_code, mode = parts[1], parts[2]
    match_type = (parts[3] if len(parts) > 3 else MatchType.COMPETITIVE).upper()

    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "ساخت پارتی فقط داخل گروه ممکنه 🍏", show_alert=True)
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
                match_type=match_type,
            )
        except (ActiveSessionExistsException, InvalidActionException) as e:
            await safe_answer_callback(callback, e.message, show_alert=True)
            return

        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=True, is_creator=True)
        await safe_answer_callback(
            callback,
            "پارتی دوستانه ساخته شد! 🍏" if match_type == MatchType.FRIENDLY
            else "پارتی با موفقیت ایجاد شد! 🍏",
        )
        if callback.message:
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("view_party:"))
async def handle_view_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """View details of a party only from its owning Telegram group."""
    try:
        party_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    user = callback.from_user
    chat = callback.message.chat if callback.message else None

    async with get_session() as session:
        party = await PartyService.get_party_by_id(session, party_id)
        if (
            not party
            or party.status == PartyStatus.CANCELLED
            or chat is None
            or chat.type not in ("group", "supergroup")
            or party.group_id != chat.id
        ):
            await safe_answer_callback(callback, "این پارتی در این گروه در دسترس نیست 🍏", show_alert=True)
            return

        is_member = any(m.user_id == user.id for m in party.members) if user else False
        is_creator = bool(user and party.creator_user_id == user.id)
        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=is_member, is_creator=is_creator)
        await safe_answer_callback(callback)
        if callback.message:
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("join_party:"))
async def handle_join_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Join a Competitive party or choose a side in a Friendly party."""
    parts = (callback.data or "").split(":")
    try:
        party_id = int(parts[1])
        team = int(parts[2]) if len(parts) > 2 else None
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "این پارتی فقط از گروه خودش قابل پیوستنه 🍏", show_alert=True)
        return

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        try:
            party = await PartyService.join_party(
                session, party_id, user.id, team=team, group_id=chat.id
            )
        except (ActiveSessionExistsException, PartyFullException, PartyNotFoundException, InvalidActionException) as e:
            await safe_answer_callback(callback, e.message, show_alert=True)
            return

        text = render_party_card(party)
        kb = get_party_action_keyboard(
            party, is_member=True, is_creator=party.creator_user_id == user.id
        )
        await safe_answer_callback(callback, "به پارتی اضافه شدی! 🍏✨")
        if callback.message:
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("fill_friendly_bots:"))
async def handle_fill_friendly_bots(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Creator-authorized Friendly slot fill; all checks live in the service."""
    try:
        party_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "این پارتی فقط از گروه خودش قابل تغییره 🍏", show_alert=True)
        return

    async with get_session() as session:
        try:
            party = await PartyService.fill_bot_slots(
                session, party_id, user.id, group_id=chat.id
            )
        except (PartyFullException, PartyNotFoundException, InvalidActionException) as e:
            await safe_answer_callback(callback, e.message, show_alert=True)
            return
        text = render_party_card(party)
        kb = get_party_action_keyboard(party, is_member=True, is_creator=True)
        await safe_answer_callback(callback, "جای خالی‌ها با بات‌های سیب پر شد 🤖🍏")
        if callback.message:
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("start_friendly:"))
async def handle_start_friendly(callback: CallbackQuery, bot: Bot):
    """Start the single shared board from a full same-group Friendly party."""
    import json

    from sib.games.registry import get_game_engine
    from sib.services.friendly_match import FriendlyMatchService
    from sib.services.telegram_ui import safe_send_screen, with_refresh_button

    try:
        party_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    user = callback.from_user
    message = callback.message
    chat = message.chat if message else None
    if not user or not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "این بازی فقط داخل گروه قابل شروعه 🍏", show_alert=True)
        return

    async with get_session() as session:
        try:
            match = await FriendlyMatchService.start_party(
                session, party_id, user.id, group_id=chat.id
            )
        except (InvalidActionException, PartyNotFoundException) as e:
            await safe_answer_callback(callback, e.message, show_alert=True)
            return
        source_message_id = getattr(message, "message_id", None)
        match.group1_message_id = source_message_id
        await session.commit()

    state = json.loads(match.game_state or "{}")
    engine = get_game_engine(match.game.code)
    board_player_id = state.get("current_player_id") or user.id
    board_text = engine.render_for_group(state, chat.id, board_player_id)
    keyboard = with_refresh_button(
        engine.get_keyboard(state, match.id, board_player_id), match.id
    )
    await safe_answer_callback(callback, "بازی دوستانه شروع شد! 🍏🎮")
    rendered = await safe_edit_callback_message(
        callback, bot, board_text, reply_markup=keyboard
    )
    if rendered:
        live_message_id = getattr(callback.message, "message_id", None)
    else:
        sent = await safe_send_screen(
            bot, chat.id, board_text, reply_markup=keyboard
        )
        live_message_id = getattr(sent, "message_id", None) if sent else None

    if live_message_id:
        async with get_session() as session:
            saved_match = await session.get(Match, match.id)
            if saved_match:
                saved_match.group1_message_id = live_message_id

    after_start = getattr(engine, "after_start", None)
    if after_start is not None:
        try:
            await after_start(bot, match.id)
        except Exception:
            # The match is already committed; post-start presentation hooks
            # must never make a successful start look like a failed one.
            pass



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
    """Leave an open party from its owning Telegram group."""
    try:
        party_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "این پارتی فقط از گروه خودش قابل تغییره 🍏", show_alert=True)
        return

    async with get_session() as session:
        try:
            party, was_cancelled = await PartyService.leave_party(
                session, party_id, user.id, group_id=chat.id
            )
        except (PartyNotFoundException, InvalidActionException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        if was_cancelled:
            await callback.answer("شما خارج شدید و چون کسی نمانده بود، پارتی لغو شد 🍏")
            await _cancel_and_restore_menu(callback, "❌ **این پارتی لغو شد.**", bot)
        else:
            await callback.answer("از پارتی خارج شدی 🍏")
            text = render_party_card(party)
            kb = get_party_action_keyboard(
                party, is_member=False, is_creator=party.creator_user_id == user.id
            )
            if callback.message:
                await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("cancel_party:"))
async def handle_cancel_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Cancel an open/full party from its owning Telegram group."""
    try:
        party_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return
    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat or chat.type not in ("group", "supergroup"):
        await safe_answer_callback(callback, "این پارتی فقط از گروه خودش قابل تغییره 🍏", show_alert=True)
        return

    async with get_session() as session:
        try:
            await PartyService.cancel_party(
                session, party_id, user.id, group_id=chat.id
            )
        except (PartyNotFoundException, InvalidActionException) as e:
            await callback.answer(e.message, show_alert=True)
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
