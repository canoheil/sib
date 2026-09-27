"""Apple Battle (نبرد سیبی) handlers — shared-matchmaking flow.

UX (locked decisions):
  1. pick a battle card (loadout)        → ``ab_pick:{card_id}``
  2. press "یافتن حریف" (Find Opponent)  → ``ab_find``  → REAL shared queue
  3. wait online (searching screen)      → ``ab_cancel`` is the ONLY way out
  4. match found → shared acceptance → staged narrative battle → reward

Back/home buttons are NAVIGATION ONLY and never dequeue an active search.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy.orm import selectinload

from sib.core.errors import (
    ActiveSessionExistsException,
    InvalidActionException,
    PartyNotFoundException,
)
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import AppleBattle, AppleBattleStatus, Character, PartyStatus, PlayerCard
from sib.services.apple_battle import AppleBattleService, BattleError
from sib.services.player_cards import PlayerCardService
from sib.services.nav import back_label_for, back_target, parse_callback, with_origin
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="apple_battle")


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_row(origin: str | None) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=back_label_for(origin), callback_data=back_target(origin))]


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


async def _card_names(session, cards) -> dict[int, str]:
    names: dict[int, str] = {}
    for card in cards:
        character = await session.get(Character, card.character_id)
        names[card.id] = character.name_fa if character else "کارت"
    return names


async def _select_view(user_id: int, origin: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Screen 1: choose the battle card (loadout)."""
    async with get_session() as session:
        cards = await PlayerCardService.list_cards(session, user_id)
        names = await _card_names(session, cards)
        battle_count = await session.scalar(
            __import__("sqlalchemy").select(
                __import__("sqlalchemy").func.count(AppleBattle.id)
            ).where(
                ((AppleBattle.player1_id == user_id) | (AppleBattle.player2_id == user_id))
            )
        )

    if not cards:
        return (
            "🍎 **نبرد سیبی** ⚔️\n\n"
            "برای شرکت در نبرد به کارت نیاز دارید! ابتدا از فروشگاه 🏪 یک شخصیت بخرید.",
            _kb(
                [
                    [InlineKeyboardButton(text="🏪 فروشگاه", callback_data="shop_refresh")],
                    _back_row(origin),
                ]
            ),
        )

    lines = [
        "🍎 **نبرد سیبی** ⚔️",
        "«یک مسابقه، یک برنده، یک نهال!»",
        "",
        f"کارت‌های شما: {len(cards)} | نبردهای ثبت‌شده: {battle_count or 0}",
        "",
        "کارت رزمی خود را انتخاب کنید:",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for card in cards[:10]:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"⚔️ {names.get(card.id, 'کارت')} (سطح {card.level})",
                    callback_data=f"ab_pick:{card.id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="🏆 لیدربورد نبرد", callback_data="ab_board")])
    buttons.append(
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data=with_origin("ab_refresh", origin))]
    )
    buttons.append(_back_row(origin))
    return "\n".join(lines), _kb(buttons)


async def _ready_view(user_id: int, origin: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Screen 2: card selected — press Find Opponent."""
    from sqlalchemy import select

    async with get_session() as session:
        loadout = await AppleBattleService.get_loadout(session, user_id)
        card = await session.get(PlayerCard, loadout.card_id) if loadout else None
        if not card:
            return await _select_view(user_id, origin)
        character = await session.get(Character, card.character_id)
        name = character.name_fa if character else "کارت"

    lines = [
        "🍎 **نبرد سیبی** ⚔️",
        "",
        f"کارت انتخاب‌شده: **{name}** (سطح {card.level})",
        f"⚡ قدرت {card.power} | 💨 سرعت {card.speed} | 🧠 هوش {card.intelligence}",
        "",
        "برای ورود به صف جستجوی حریف، دکمه زیر را بزنید.",
    ]
    kb = _kb(
        [
            [InlineKeyboardButton(text="🔍 یافتن حریف", callback_data="ab_find")],
            [InlineKeyboardButton(text="↩️ تغییر کارت", callback_data=with_origin("ab_change", origin))],
            [InlineKeyboardButton(text="🏆 لیدربورد نبرد", callback_data="ab_board")],
            _back_row(origin),
        ]
    )
    return "\n".join(lines), kb


def _searching_view(party_id: int, origin: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Screen 3: waiting for a real opponent in the shared queue."""
    lines = [
        "🍎 **نبرد سیبی** ⚔️",
        "",
        "🔍 **در حال جستجوی حریف از گروه‌های تلگرام...** 🍏",
        "",
        "به‌محض پیدا شدن حریف، نبرد آغاز می‌شود.",
        "⏳ لطفاً منتظر بمانید...",
    ]
    kb = _kb(
        [
            [InlineKeyboardButton(text="✖️ لغو جستجو", callback_data="ab_cancel")],
            [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="ab_status")],
            _back_row(origin),
        ]
    )
    return "\n".join(lines), kb


async def battle_entry_view(user_id: int, origin: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Entry screen used by the games menu button (select or ready)."""
    async with get_session() as session:
        loadout = await AppleBattleService.get_loadout(session, user_id)
    if loadout:
        return await _ready_view(user_id, origin=origin)
    return await _select_view(user_id, origin=origin)


async def _ensure_battle_party(session, group_id: int, user_id: int):
    """Reuse the user's solo battle party in this group, or create one."""
    from types import SimpleNamespace

    from sib.services.group import GroupService
    from sib.services.party import PartyService

    await GroupService.get_or_create_group(
        session, SimpleNamespace(id=group_id, title=None, type="supergroup", username=None)
    )
    parties = await PartyService.get_active_parties_for_group(session, group_id)
    for p in parties:
        if p.game and p.game.code == "apple_battle" and p.mode == "solo":
            if any(m.user_id == user_id for m in p.members):
                return p
    return await PartyService.create_party(
        session, group_id, user_id, "apple_battle", "solo"
    )


async def _find_my_battle_party(session, group_id: int, user_id: int):
    """Find the user's apple_battle party in this group IN ANY live status.

    Includes IN_QUEUE/IN_MATCH (which the generic active-parties listing
    deliberately excludes) so cancel/status can always reach the party.
    """
    from sqlalchemy import select as sa_select

    from sib.database.models import Party, PartyMember

    rows = await session.scalars(
        sa_select(Party)
        .join(PartyMember, PartyMember.party_id == Party.id)
        .where(PartyMember.user_id == user_id)
        .where(Party.group_id == group_id)
        .where(Party.status.in_((PartyStatus.OPEN, PartyStatus.FULL, PartyStatus.IN_QUEUE, PartyStatus.IN_MATCH)))
        .options(selectinload(Party.members), selectinload(Party.game))
    )
    for p in rows.all():
        if p.game and p.game.code == "apple_battle":
            return p
    return None


# ---------------------------------------------------------------------------
# Commands / screens
# ---------------------------------------------------------------------------


@router.message(Command("battle"))
async def cmd_battle(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    text, kb = await battle_entry_view(user.id)
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("ab_refresh"))
async def handle_ab_refresh(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    _base, origin = parse_callback(callback.data)
    text, kb = await battle_entry_view(user.id, origin=origin)
    await safe_answer_callback(callback, "بروزرسانی شد ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Flow: pick card → find opponent → queue / match
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("ab_pick:"))
async def handle_ab_pick(callback: CallbackQuery, bot: Bot):
    base, origin = parse_callback(callback.data)
    card_id = int(base.split(":")[1])
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        card = await session.get(PlayerCard, card_id)
        if not card or card.user_id != user.id:
            await safe_answer_callback(callback, "این کارت متعلق به شما نیست 🍏", show_alert=True)
            return
        await AppleBattleService.set_loadout(session, user.id, card.id)

    text, kb = await _ready_view(user.id, origin=origin)
    await safe_answer_callback(callback, "کارت انتخاب شد ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("ab_change"))
async def handle_ab_change(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    _base, origin = parse_callback(callback.data)
    async with get_session() as session:
        await AppleBattleService.dequeue(session, user.id)
    text, kb = await _select_view(user.id, origin=origin)
    await safe_answer_callback(callback, "کارت جدید را انتخاب کنید ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "ab_find")
async def handle_ab_find(callback: CallbackQuery, bot: Bot):
    """Enter the REAL shared matchmaking queue (solo party)."""
    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat:
        return

    from sib.handlers.matchmaking.renderer import render_match_found_for_group
    from sib.keyboards.matchmaking import get_acceptance_keyboard
    from sib.matchmaking.matcher import MatchmakingService

    async with get_session() as session:
        try:
            party = await _ensure_battle_party(session, chat.id, user.id)
            await MatchmakingService.enqueue_party(session, party.id, user.id)
            match = await MatchmakingService.find_and_create_match(session, party.id)
        except (InvalidActionException, PartyNotFoundException, ActiveSessionExistsException) as e:
            await safe_answer_callback(callback, getattr(e, "message", str(e)), show_alert=True)
            return
        except BattleError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

        if not match:
            await safe_answer_callback(callback, "وارد صف نبرد شدی! ⚔️")
            if callback.message:
                text, kb = _searching_view(party.id)
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return

        # Match found immediately — precompute the battle BEFORE narration.
        state = await AppleBattleService.build_match_state(session, match)
        if state is None:
            await safe_answer_callback(
                callback, "کارت یکی از بازیکنان در دسترس نیست؛ دوباره تلاش کنید 🍏", show_alert=True
            )
            return
        import json as _json

        match.game_state = _json.dumps(state, ensure_ascii=False)
        await session.flush()

        await safe_answer_callback(callback, "حریف پیدا شد! ⚔️✨")

        # Match-found screens show each side's OWN card art (photo+caption;
        # falls back to text when art is unavailable).
        from sib.services.character_art import photo_arg, resolve_character_photo
        from sib.services.telegram_ui import safe_send_screen

        async def _group_art(group_id):
            loadout_id = (
                match.group1_loadout_id
                if group_id == match.group1_id
                else match.group2_loadout_id
            )
            loadout = await session.get(PlayerCard, loadout_id) if loadout_id else None
            character = (
                await session.get(Character, loadout.character_id) if loadout else None
            )
            if character is None:
                return None
            file_id, fs_path = await resolve_character_photo(session, character)
            return photo_arg(file_id, fs_path)

        # 1. Group 1 message
        text_g1 = render_match_found_for_group(match, match.group1_id)
        kb_g1 = get_acceptance_keyboard(match.id, has_accepted=False)
        msg1 = await safe_send_screen(
            bot, match.group1_id, text_g1,
            photo=await _group_art(match.group1_id), reply_markup=kb_g1,
        )
        match.group1_message_id = msg1.message_id
        if callback.message and callback.message.chat.id == match.group1_id:
            # The old searching screen cannot turn into a photo — remove it.
            try:
                await bot.delete_message(match.group1_id, callback.message.message_id)
            except Exception:
                pass

        # 2. Group 2 message
        text_g2 = render_match_found_for_group(match, match.group2_id)
        kb_g2 = get_acceptance_keyboard(match.id, has_accepted=False)
        msg2 = await safe_send_screen(
            bot, match.group2_id, text_g2,
            photo=await _group_art(match.group2_id), reply_markup=kb_g2,
        )
        match.group2_message_id = msg2.message_id

        await session.flush()


@router.callback_query(F.data == "ab_cancel")
async def handle_ab_cancel(callback: CallbackQuery, bot: Bot):
    """EXPLICIT cancel: the only action that dequeues (locked decision)."""
    user = callback.from_user
    if not user:
        return

    from sib.matchmaking.matcher import MatchmakingService

    async with get_session() as session:
        try:
            party = await _find_my_battle_party(
                session, callback.message.chat.id if callback.message else 0, user.id
            )
            if party and party.status == PartyStatus.IN_QUEUE:
                await MatchmakingService.cancel_matchmaking(session, party.id, user.id)
            await AppleBattleService.dequeue(session, user.id)
        except (InvalidActionException, PartyNotFoundException) as e:
            await safe_answer_callback(callback, e.message, show_alert=True)
            return

    text, kb = await _select_view(user.id)
    await safe_answer_callback(callback, "جستجوی حریف لغو شد 🍏")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "ab_status")
async def handle_ab_status(callback: CallbackQuery, bot: Bot):
    """Refresh the searching screen (queue is untouched)."""
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        party = await _find_my_battle_party(
            session, callback.message.chat.id if callback.message else 0, user.id
        )

    if party and party.status == PartyStatus.IN_QUEUE:
        text, kb = _searching_view(party.id)
        await safe_answer_callback(callback, "هنوز در جستجو هستید ⏳")
    else:
        loadout = None
        async with get_session() as session:
            loadout = await AppleBattleService.get_loadout(session, user.id)
        if loadout:
            text, kb = await _ready_view(user.id)
            await safe_answer_callback(callback, "آماده‌ی نبرد ⚔️")
        else:
            text, kb = await _select_view(user.id)
            await safe_answer_callback(callback, "کارت خود را انتخاب کنید ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "ab_board")
async def handle_ab_board(callback: CallbackQuery, bot: Bot):
    async with get_session() as session:
        board = await AppleBattleService.battle_leaderboard(session)
        names = []
        for user_id, wins, total in board[:15]:
            names.append(f"🏆 `{user_id}` — {wins} برد از {total} نبرد")

    text = "🏆 **لیدربورد نبرد سیبی**\n\n"
    text += "\n".join(names) if names else "هنوز نبردی ثبت نشده است."
    kb = _kb(
        [
            [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="ab_board")],
            [InlineKeyboardButton(text="↩️ بازگشت به نبرد سیبی", callback_data="apple_battle")],
        ]
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Profile (Phase 15)
# ---------------------------------------------------------------------------


@router.message(Command("profile"))
async def cmd_profile(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return

    from sqlalchemy import func, select

    from sib.services.apple_wallet import AppleWalletService
    from sib.services.seedling import SeedlingService

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
                ((AppleBattle.player1_id == user.id) | (AppleBattle.player2_id == user.id))
                & (AppleBattle.status == AppleBattleStatus.COMPLETED)
            )
        )
        favorite = next((c for c in cards if c.is_favorite), None)
        fav_text = ""
        if favorite:
            character = await session.get(Character, favorite.character_id)
            name = character.name_fa if character else "?"
            fav_text = f"⭐ کارت اصلی: **{name}** (سطح {favorite.level})\n"

    lines = [
        f"👤 **پروفایل {user.full_name}**",
        "",
        f"🌱 نهال‌ها: `{seedlings}` | 🍎 سیب‌ها: `{apples}`",
        f"🎴 کارت‌ها: `{len(cards)}`",
        fav_text.strip() if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    from sib.keyboards.common import get_profile_keyboard

    fav_photo = None
    if favorite:
        async with get_session() as session:
            character = await session.get(Character, favorite.character_id)
            if character is not None:
                from sib.services.character_art import photo_arg, resolve_character_photo

                file_id, fs_path = await resolve_character_photo(session, character)
                fav_photo = photo_arg(file_id, fs_path)
    text = "\n".join(lines)
    kb = get_profile_keyboard()
    if fav_photo is not None:
        try:
            await message.reply_photo(
                fav_photo, caption=text, reply_markup=kb, parse_mode="Markdown"
            )
            return
        except Exception:
            pass
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")
