"""Find Match regression: real two-player callback -> DB -> match-found UI."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery
from sqlalchemy import select

from sib.database.models import Match, MatchState, MatchmakingQueue, Party, PartyStatus, PlayerCard
from sib.handlers.apple_battle import handle_ab_find, handle_ab_pick
from sib.handlers.matchmaking.handlers import handle_accept_match
from sib.services.apple_battle import AppleBattleService
from sib.tests.test_apple_battle import _card
from sib.tests.test_parties import create_test_group, create_test_user
from sib.tests.test_screen_transitions import FakeBot


def _callback(message, user_id, data):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = MagicMock(id=user_id, is_bot=False, first_name="بازیکن")
    cb.message = message
    cb.answer = AsyncMock()
    return cb


def _callbacks(message):
    return {button.callback_data for row in message.reply_markup.inline_keyboard
            for button in row if button.callback_data}


@pytest.mark.asyncio
async def test_two_players_find_match_and_see_card_art_and_acceptance(db_session):
    """The second press must not crash AFTER the shared matcher has found a match.

    Both loadouts are consumed when the precomputed battle state is built.
    The match-found UI must still resolve each player's actual card and
    create a working acceptance screen in both Telegram chats.
    """
    g1 = await create_test_group(db_session, "گروه نبرد یک")
    g2 = await create_test_group(db_session, "گروه نبرد دو")
    a = await create_test_user(db_session, "سیب اول")
    b = await create_test_user(db_session, "سیب دوم")
    card_a = await _card(db_session, a, "sib_sorkh")
    card_b = await _card(db_session, b, "sib_sabz")
    a_id, b_id = a.id, b.id
    g1_id, g2_id = g1.id, g2.id
    card_a_id, card_b_id = card_a.id, card_b.id
    await db_session.commit()

    bot = FakeBot()
    first_msg = await bot.send_message(g1_id, "نبرد سیبی")
    second_msg = await bot.send_message(g2_id, "نبرد سیبی")
    cb1 = _callback(first_msg, a_id, f"ab_pick:{card_a_id}")
    cb2 = _callback(second_msg, b_id, f"ab_pick:{card_b_id}")

    await handle_ab_pick(cb1, bot)
    await handle_ab_pick(cb2, bot)
    assert "ab_find" in _callbacks(cb1.message)
    assert "ab_find" in _callbacks(cb2.message)

    cb1.data = "ab_find"
    await handle_ab_find(cb1, bot)
    await db_session.rollback()
    assert "ab_cancel" in _callbacks(cb1.message)
    queue = list((await db_session.scalars(select(MatchmakingQueue))).all())
    assert len(queue) == 1 and queue[0].is_active
    assert (await AppleBattleService.get_loadout(db_session, a_id)).card_id == card_a_id
    await db_session.rollback()

    cb2.data = "ab_find"
    await handle_ab_find(cb2, bot)
    await db_session.rollback()

    match = await db_session.scalar(select(Match))
    assert match is not None and match.state == MatchState.WAITING_FOR_ACCEPTANCE
    assert {match.group1_id, match.group2_id} == {g1_id, g2_id}
    state = json.loads(match.game_state)
    assert {int(uid) for uid in state["cards"]} == {a_id, b_id}
    assert state["cards"][str(a_id)]["card_id"] == card_a_id
    assert state["cards"][str(b_id)]["card_id"] == card_b_id
    assert state["plan"] and state["battle"]["winner_id"] in (a_id, b_id)
    assert await AppleBattleService.get_loadout(db_session, a_id) is None
    assert await AppleBattleService.get_loadout(db_session, b_id) is None
    assert not (await db_session.scalars(select(MatchmakingQueue))).all()[0].is_active
    assert {p.status for p in (await db_session.scalars(select(Party))).all()} == {PartyStatus.IN_MATCH}

    for group_id, msg_id, player_name, own_card_art in (
        (match.group1_id, match.group1_message_id, "سیب دوم", "sib_sabz.jpg"),
        (match.group2_id, match.group2_message_id, "سیب اول", "sib_sorkh.jpg"),
    ):
        assert msg_id is not None
        displayed = bot.messages[(group_id, msg_id)]
        assert not displayed.deleted
        assert displayed.kind == "photo" and displayed.photo is not None
        assert Path(displayed.photo.path).name == own_card_art
        assert "حریف پیدا شد" in displayed.caption
        assert player_name in displayed.caption
        assert {f"accept_match:{match.id}", f"reject_match:{match.id}"} <= _callbacks(displayed)

    # The callback handle on the second player's panel must follow the
    # actual match-found message, including if a type replacement occurred.
    assert cb2.message.message_id == match.group1_message_id
    match_id, group1_id = match.id, match.group1_id
    await db_session.rollback()

    # Acceptance is wired to the persisted match rather than a stale message.
    accepted_message = MagicMock()
    accepted_message.chat.id = group1_id
    accepted_message.edit_reply_markup = AsyncMock()
    accept = _callback(accepted_message, b_id, f"accept_match:{match_id}")
    await handle_accept_match(accept, bot)
    accepted_message.edit_reply_markup.assert_awaited_once()
    kb = accepted_message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert f"reject_match:{match_id}" in {b.callback_data for row in kb.inline_keyboard for b in row}
    await db_session.rollback()
    assert (await db_session.get(Match, match_id)).group1_accepted is True
