"""Group menu: 🎮 Games and 📖 Guide entries + navigation safety.

Task requirement: the group menu exposes the games menu and the guide,
reusing the existing navigation convention (Back / Home / Refresh), and
navigation must NEVER cancel an active match or party.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import select

from sib.database.models import Game, Match, MatchPlayer, MatchState, Party, PartyMember, PartyStatus
from sib.handlers.party.handlers import (
    build_active_games_menu,
    build_games_menu,
)
from sib.handlers.start import (
    handle_help_menu,
    handle_help_navigation,
    handle_help_page,
    handle_main_menu,
)
from sib.keyboards.common import get_group_welcome_keyboard
from sib.keyboards.party import (
    get_active_parties_keyboard,
    get_games_menu_keyboard,
    get_party_action_keyboard,
)
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user

NAV_ONLY = {
    "main_menu",
    "group_games_panel",
    "group_active_games",
    "help_menu",
    "apple_cards",
    "apple_shop",
    "apple_profile",
    "apple_trade",
    "create_new_party_menu",
    "help_menu:group_games_panel",
    "help_menu:main_menu",
}


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _callback(user, data: str, *, chat_id: int = -100700, chat_type: str = "supergroup") -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name or "کاربر")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type=chat_type, title="گروه")
    cb.message.message_id = 42
    cb.message.text = "panel"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    return bot


def _edit_kb(cb, bot):
    call = cb.message.edit_text.call_args or bot.edit_message_text.call_args
    assert call is not None, "screen must be rendered"
    return call.kwargs.get("reply_markup")


# ---------------------------------------------------------------------------
# Entries exist
# ---------------------------------------------------------------------------


def test_group_main_menu_exposes_games_and_guide():
    kb = get_group_welcome_keyboard()
    by_label = {b.text: b.callback_data for b in _buttons(kb)}
    assert any("🎮" in label for label in by_label)
    assert any("📖" in label for label in by_label)
    assert "group_games_panel" in by_label.values()
    assert "help_menu" in by_label.values()
    # Card trading / My Cards are reached through «👤 پروفایل من» and the
    # active-games screen through «🎮 بازی‌ها» - the main menu keeps ONE
    # entry per feature (see test_main_menu_layout.py).
    assert not any("🔁" in label for label in by_label)


def test_back_buttons_are_navigation_only():
    """Every Back/Home button on the navigation chain only navigates."""
    keyboards = [
        get_group_welcome_keyboard(),
        get_games_menu_keyboard([], active_count=2),
        get_active_parties_keyboard([]),
    ]
    for kb in keyboards:
        for b in _buttons(kb):
            text = b.text or ""
            if text.startswith(("↩️", "🏠", "📚", "🔄")):
                assert b.callback_data in NAV_ONLY or b.callback_data.startswith(
                    ("view_party:", "trade", "apple_")
                ), f"{text} must not perform a destructive action ({b.callback_data})"
                assert not b.callback_data.startswith(
                    ("cancel_", "leave_", "forfeit_", "reject_")
                )


# ---------------------------------------------------------------------------
# Guide navigation (context aware, never destructive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guide_from_group_back_returns_to_main_menu(db_session):
    user = await create_test_user(db_session, "راهنما")
    bot = _bot()

    cb = _callback(user, "help_menu")
    await handle_help_menu(cb, bot)
    kb = _edit_kb(cb, bot)
    targets = [b.callback_data for b in _buttons(kb)]
    assert "main_menu" in targets  # one press home


@pytest.mark.asyncio
async def test_guide_page_keeps_its_origin(db_session):
    user = await create_test_user(db_session, "راهنمای صفحه")
    bot = _bot()

    cb = _callback(user, "help:section:social:group_games_panel")
    await handle_help_navigation(cb, bot)
    kb = _edit_kb(cb, bot)
    index_buttons = [b for b in _buttons(kb) if b.callback_data.startswith("help:index")]
    assert index_buttons
    assert index_buttons[0].callback_data == "help:index:group_games_panel"

    # ... and the index itself carries the origin to the pages again.
    cb2 = _callback(user, "help_menu:group_games_panel")
    await handle_help_menu(cb2, bot)
    kb2 = _edit_kb(cb2, bot)
    page_callbacks = [b.callback_data for b in _buttons(kb2) if b.callback_data.startswith("help:section:")]
    assert page_callbacks and all(c.endswith(":group_games_panel") for c in page_callbacks)


# ---------------------------------------------------------------------------
# Navigation never touches a live match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_while_match_is_active_preserves_game_state(db_session):
    group = await create_test_group(db_session, "گروه نبرد")
    user = await create_test_user(db_session, "بازیکن نبرد")
    game = await db_session.scalar(select(Game).where(Game.code == "rock_paper_scissors"))
    party = Party(
        group_id=group.id,
        game_id=game.id,
        mode="solo",
        max_players=1,
        current_players_count=1,
        status=PartyStatus.IN_MATCH,
        creator_user_id=user.id,
    )
    db_session.add(party)
    await db_session.flush()
    db_session.add(PartyMember(party_id=party.id, user_id=user.id))
    match = Match(
        game_id=game.id,
        mode="solo",
        state=MatchState.ACTIVE,
        group1_id=group.id,
        party1_id=party.id,
    )
    db_session.add(match)
    await db_session.flush()
    db_session.add(
        MatchPlayer(
            match_id=match.id,
            user_id=user.id,
            group_id=group.id,
            team=1,
            turn_order=1,
            is_active=True,
            has_accepted=True,
        )
    )
    await db_session.commit()
    party_id, match_id = party.id, match.id

    bot = _bot()
    cb = _callback(user, "main_menu")
    await handle_main_menu(cb, bot)

    db_session.expunge_all()
    party_after = await db_session.get(Party, party_id)
    match_after = await db_session.get(Match, match_id)
    assert party_after.status == PartyStatus.IN_MATCH
    assert match_after.state == MatchState.ACTIVE
    players = list(
        (await db_session.scalars(select(MatchPlayer).where(MatchPlayer.match_id == match_id))).all()
    )
    assert all(p.is_active for p in players)


@pytest.mark.asyncio
async def test_games_and_active_screens_keep_live_match_visible(db_session):
    group = await create_test_group(db_session, "گروه نمایش")
    user = await create_test_user(db_session, "بازیکن نمایش")
    party = await PartyService.create_party(
        db_session, group.id, user.id, "rock_paper_scissors", "solo"
    )
    party.status = PartyStatus.IN_MATCH
    await db_session.commit()

    text, kb = await build_games_menu(db_session, group.id)
    assert "group_active_games" in [b.callback_data for b in _buttons(kb)]

    text, kb = await build_active_games_menu(db_session, group.id)
    assert f"view_party:{party.id}" in [b.callback_data for b in _buttons(kb)]
    assert "مسابقه" in {b.callback_data: b.text for b in _buttons(kb)}[f"view_party:{party.id}"]

    # The party screen for a live match has no destructive action, only
    # refresh + back (the match screen owns the match).
    party_kb = get_party_action_keyboard(party, is_member=True)
    callbacks = [b.callback_data for b in _buttons(party_kb)]
    assert not any(c.startswith(("cancel_party:", "leave_party:", "forfeit_")) for c in callbacks)
    assert "group_active_games" in callbacks
