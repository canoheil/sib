"""Apple Battle (نبرد سیبی) handlers — shared-matchmaking flow.

UX (locked decisions):
  1. pick a battle card (loadout)        → ``ab_pick:{card_id}``
  2. press "یافتن حریف" (Find Opponent)  → ``ab_find``  → REAL shared queue
  3. wait online (searching screen)      → ``ab_cancel`` is the ONLY way out
  4. match found → shared acceptance → cinematic narration as SEPARATE
     temporary photo messages (Phase 2A) → final report → reward

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
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    resolve_personal_actor,
)
from sib.services.player_cards import PlayerCardService
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.nav import back_label_for, back_target, parse_callback, with_origin
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="apple_battle")


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _battle_actor(callback: CallbackQuery) -> int:
    """Resolve the authorized actor for an apple-battle callback.

    The battle HUB is a personal panel (``apple_battle``); only the
    opener may pick a loadout / queue / cancel. Defense-in-depth beyond
    the outer middleware - a stale, replayed or forged button is never
    allowed to affect another user's queue or loadout.
    """
    user = callback.from_user
    if user is None:
        return 0
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        try:
            await callback.answer(LOCKED_MESSAGE, show_alert=True)
        except Exception:  # pragma: no cover - defensive
            pass
        return 0
    return actor_id


def _back_row(origin: str | None) -> list[InlineKeyboardButton]:
    return [danger_button(text=back_label_for(origin), callback_data=back_target(origin))]


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


async def _card_names(session, cards) -> dict[int, str]:
    """Batch-load card->character names (one query, no per-card reads)."""
    from sqlalchemy import select as sa_select

    character_ids = sorted({c.character_id for c in cards})
    if not character_ids:
        return {}
    characters = {
        ch.id: ch
        for ch in (
            await session.scalars(
                sa_select(Character).where(Character.id.in_(character_ids))
            )
        ).all()
    }
    return {
        card.id: (characters[card.character_id].name_fa if card.character_id in characters else "کارت")
        for card in cards
    }


async def _select_view(user_id: int, origin: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Screen 1: choose the battle card (loadout)."""
    async with get_session() as session:
        cards = await PlayerCardService.list_cards(session, user_id)
        names = await _card_names(session, cards)
        from sqlalchemy import func, select as sa_select

        battle_count = await session.scalar(
            sa_select(func.count(AppleBattle.id)).where(
                ((AppleBattle.player1_id == user_id) | (AppleBattle.player2_id == user_id))
            )
        )

    if not cards:
        return (
            "🍎 **نبرد سیبی** ⚔️\n\n"
            "برای شرکت در نبرد به کارت نیاز دارید! ابتدا از فروشگاه 🏪 یک شخصیت بخرید.",
            _kb(
                [
                    [primary_button(text="🏪 فروشگاه", callback_data="shop_refresh")],
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
                primary_button(
                    text=f"⚔️ {names.get(card.id, 'کارت')} (سطح {card.level})",
                    callback_data=f"ab_pick:{card.id}",
                )
            ]
        )
    buttons.append(
        [primary_button(text="🔄 بروزرسانی", callback_data=with_origin("ab_refresh", origin))]
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
        stats = AppleBattleService._card_stats(card, character)

    lines = [
        "🍎 **نبرد سیبی** ⚔️",
        "",
        f"کارت انتخاب‌شده: **{name}** (سطح {card.level})",
        f"⚡ قدرت {stats.power} | 💨 سرعت {stats.speed} | 🧠 هوش {stats.intelligence}",
        "",
        "برای ورود به صف جستجوی حریف، دکمه زیر را بزنید.",
    ]
    kb = _kb(
        [
            [success_button(text="🔍 یافتن حریف", callback_data="ab_find")],
            [danger_button(text="↩️ تغییر کارت", callback_data=with_origin("ab_change", origin))],
            [primary_button(text="🏆 لیدربورد نبرد", callback_data="ab_board")],
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
        "همین که حریف پیدا بشه، نبرد شروع می‌شه.",
        "⏳ لطفاً منتظر بمانید...",
    ]
    kb = _kb(
        [
            [danger_button(text="✖️ لغو جستجو", callback_data="ab_cancel")],
            [primary_button(text="🔄 بروزرسانی", callback_data="ab_status")],
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
    sent = await message.reply(text, reply_markup=kb, parse_mode="Markdown")
    if sent is not None and sent.message_id:
        from sib.database.connection import get_session as _gs
        from sib.services.panel import PanelOwnershipService as _POS

        async with _gs() as session:
            await _POS.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="apple_battle",
            )
            await session.commit()


@router.callback_query(F.data.startswith("ab_refresh"))
async def handle_ab_refresh(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    actor = await _battle_actor(callback)
    if not actor or actor != user.id:
        return
    _base, origin = parse_callback(callback.data)
    text, kb = await battle_entry_view(actor, origin=origin)
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
    actor = await _battle_actor(callback)
    if not actor or actor != user.id:
        return

    async with get_session() as session:
        card = await session.get(PlayerCard, card_id)
        if not card or card.user_id != actor:
            await safe_answer_callback(callback, "این کارت متعلق به شما نیست 🍏", show_alert=True)
            return
        await AppleBattleService.set_loadout(session, actor, card.id)

    text, kb = await _ready_view(actor, origin=origin)
    await safe_answer_callback(callback, "کارت انتخاب شد ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("ab_change"))
async def handle_ab_change(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    actor = await _battle_actor(callback)
    if not actor or actor != user.id:
        return
    _base, origin = parse_callback(callback.data)
    async with get_session() as session:
        await AppleBattleService.dequeue(session, actor)
    text, kb = await _select_view(actor, origin=origin)
    await safe_answer_callback(callback, "کارت جدید را انتخاب کنید ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "ab_find")
async def handle_ab_find(callback: CallbackQuery, bot: Bot):
    """Enter the REAL shared matchmaking queue (solo party)."""
    user = callback.from_user
    chat = callback.message.chat if callback.message else None
    if not user or not chat:
        return
    actor = await _battle_actor(callback)
    if not actor or actor != user.id:
        return

    from sib.handlers.matchmaking.renderer import render_match_found_for_group
    from sib.keyboards.matchmaking import get_acceptance_keyboard
    from sib.matchmaking.matcher import MatchmakingService

    async with get_session() as session:
        try:
            party = await _ensure_battle_party(session, chat.id, actor)
            queue_entry = await MatchmakingService.enqueue_party(session, party.id, actor)
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
                await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
                message_id = getattr(callback.message, "message_id", None)
                if isinstance(message_id, int):
                    queue_entry.message_id = message_id
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
        from sib.services.telegram_ui import render_callback_screen, safe_send_screen

        async def _group_art(group_id):
            # build_match_state consumed both loadouts. The authoritative card
            # ids are in the precomputed state, keyed by MatchPlayer.user_id;
            # Match has no group*_loadout_id columns.
            player = next((p for p in match.players if p.group_id == group_id), None)
            card_data = state["cards"].get(str(player.user_id)) if player else None
            card = await session.get(PlayerCard, card_data["card_id"]) if card_data else None
            character = await session.get(Character, card.character_id) if card else None
            if character is None:
                return None
            file_id, fs_path = await resolve_character_photo(session, character)
            return photo_arg(file_id, fs_path)

        # 1. Group 1 message
        text_g1 = render_match_found_for_group(match, match.group1_id)
        kb_g1 = get_acceptance_keyboard(match.id, has_accepted=False)
        art_g1 = await _group_art(match.group1_id)
        if callback.message and callback.message.chat.id == match.group1_id:
            msg1 = await render_callback_screen(
                callback, bot, text_g1, photo=art_g1, reply_markup=kb_g1,
            )
        else:
            msg1 = await safe_send_screen(
                bot, match.group1_id, text_g1, photo=art_g1, reply_markup=kb_g1,
            )
        if msg1 is not None:
            match.group1_message_id = msg1.message_id

        # 2. Group 2 message
        text_g2 = render_match_found_for_group(match, match.group2_id)
        kb_g2 = get_acceptance_keyboard(match.id, has_accepted=False)
        msg2 = await safe_send_screen(
            bot, match.group2_id, text_g2,
            photo=await _group_art(match.group2_id), reply_markup=kb_g2,
        )
        if msg2 is not None:
            match.group2_message_id = msg2.message_id

        await session.flush()


@router.callback_query(F.data == "ab_cancel")
async def handle_ab_cancel(callback: CallbackQuery, bot: Bot):
    """EXPLICIT cancel: the only action that dequeues (locked decision)."""
    user = callback.from_user
    if not user:
        return
    actor = await _battle_actor(callback)
    if not actor or actor != user.id:
        return

    from sib.matchmaking.matcher import MatchmakingService

    async with get_session() as session:
        try:
            party = await _find_my_battle_party(
                session, callback.message.chat.id if callback.message else 0, actor
            )
            if party and party.status == PartyStatus.IN_QUEUE:
                await MatchmakingService.cancel_matchmaking(session, party.id, actor)
            await AppleBattleService.dequeue(session, actor)
        except (InvalidActionException, PartyNotFoundException) as e:
            await safe_answer_callback(callback, e.message, show_alert=True)
            return

    text, kb = await _select_view(actor)
    await safe_answer_callback(callback, "جستجوی حریف لغو شد 🍏")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "ab_status")
async def handle_ab_status(callback: CallbackQuery, bot: Bot):
    """Refresh the searching screen (queue is untouched)."""
    user = callback.from_user
    if not user:
        return
    actor = await _battle_actor(callback)
    if not actor or actor != user.id:
        return

    async with get_session() as session:
        party = await _find_my_battle_party(
            session, callback.message.chat.id if callback.message else 0, actor
        )

    if party and party.status == PartyStatus.IN_QUEUE:
        text, kb = _searching_view(party.id)
        await safe_answer_callback(callback, "هنوز دنبال حریف می‌گردی ⏳")
    else:
        loadout = None
        async with get_session() as session:
            loadout = await AppleBattleService.get_loadout(session, actor)
        if loadout:
            text, kb = await _ready_view(actor)
            await safe_answer_callback(callback, "آماده‌ی نبرد ⚔️")
        else:
            text, kb = await _select_view(actor)
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
    text += "\n".join(names) if names else "هنوز نبردی ثبت نشده."
    kb = _kb(
        [
            [primary_button(text="🔄 بروزرسانی", callback_data="ab_board")],
            [danger_button(text="↩️ بازگشت به نبرد سیبی", callback_data="apple_battle")],
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
    from sib.services.player_xp import PlayerXPService
    from sib.services.profile_skin import ProfileSkinService, profile_skin_label
    from sib.services.panel import PanelOwnershipService

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
                ((AppleBattle.player1_id == user.id) | (AppleBattle.player2_id == user.id))
                & (AppleBattle.status == AppleBattleStatus.COMPLETED)
            )
        )
        fav_text = ""
        if main_card is not None:
            character = await session.get(Character, main_card.character_id)
            name = character.name_fa if character else "?"
            fav_text = f"⭐ کارت اصلی: **{name}** (سطح {main_card.level})\n"

        # The ACTIVE spouse + shared Love, straight from the marriage domain.
        from sib.handlers.marriage import profile_marriage_lines

        marriage_lines = await profile_marriage_lines(session, user.id)
        account_progress = await PlayerXPService.get_progress(session, user.id)
        selected_skin = await ProfileSkinService.get_selected_skin(session, user.id)

    lines = [
        f"👤 **پروفایل {user.full_name}**",
        PlayerXPService.format_profile_progress(account_progress),
        f"🎨 **Change Skin / تغییر پوسته**: {profile_skin_label(selected_skin)}",
        "",
        f"🌱 نهال‌ها: `{seedlings}` | 🍎 سیب‌ها: `{apples}`",
        f"🎴 کارت‌ها: `{len(cards)}`",
        fav_text.strip() if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    lines.extend(marriage_lines)
    from sib.keyboards.common import get_profile_keyboard

    fav_photo = None
    if main_card is not None:
        async with get_session() as session:
            character = await session.get(Character, main_card.character_id)
            if character is not None:
                from sib.services.character_art import photo_arg, resolve_character_photo

                file_id, fs_path = await resolve_character_photo(session, character)
                fav_photo = photo_arg(file_id, fs_path)
    text = "\n".join(lines)
    kb = get_profile_keyboard()

    # Green Apple delegates to its original Level-specific art resolver.
    from sib.services.profile_artwork import ProfileArtworkResolver

    main_photo = ProfileArtworkResolver.profile_photo(
        account_progress, skin=selected_skin, fallback_photo=fav_photo
    )
    sent = None
    if main_photo is not None:
        try:
            sent = await message.reply_photo(
                main_photo, caption=text, reply_markup=kb, parse_mode="Markdown"
            )
        except Exception:
            sent = None
    if sent is None:
        sent = await message.reply(text, reply_markup=kb, parse_mode="Markdown")

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
