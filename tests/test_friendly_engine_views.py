"""Same-group Friendly rendering and public-participant authorization regressions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import select

from sib.core.errors import InvalidActionException
from sib.database.models import Match, MatchPlayer, MatchType, Party
from sib.games.battleship.engine import BattleshipEngine
from sib.games.connect_four.engine import ConnectFourEngine
from sib.games.high_card.engine import HighCardEngine
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.games.tic_tac_toe.engine import TicTacToeEngine
from sib.handlers.games import common as game_common
from sib.handlers.games.common import handle_game_refresh, process_game_action
from sib.handlers.matchmaking.handlers import handle_accept_match
from sib.matchmaking.matcher import MatchmakingService
from sib.services.friendly_match import FriendlyMatchService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


def _players(count_per_team=1, group_id=-500):
    players = []
    for team in (1, 2):
        for ordinal in range(1, count_per_team + 1):
            user_id = team * 1000 + ordinal
            players.append(
                SimpleNamespace(
                    user_id=user_id,
                    team=team,
                    group_id=group_id,
                    user=SimpleNamespace(display_name=f"بازیکن {team}-{ordinal}"),
                )
            )
    return players


def _friendly_state(engine, players, mode="solo"):
    state = engine.create_initial_state(players, mode)
    state["match_type"] = MatchType.FRIENDLY
    state["team_names"] = {"1": "تیم اول", "2": "تیم دوم"}
    return state


def _callbacks(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_friendly_rps_shared_keyboard_keeps_each_choice_private_until_reveal():
    engine = RockPaperScissorsEngine()
    players = _players()
    state = _friendly_state(engine, players)
    before = engine.render_for_group(state, -500, 1001)
    assert "بازیکن 1-1" in before and "بازیکن 2-1" in before
    assert "🗿 سنگ" not in before
    assert all(value.startswith("rps_action:") for value in _callbacks(engine.get_keyboard(state, 91, 1001)))

    state = engine.apply_action(state, 1001, {"choice": "rock"})
    waiting = engine.render_for_group(state, -500, 1002)
    assert "🗿 سنگ" not in waiting
    assert "انتخاب ثبت شده" in waiting
    assert all(value.startswith("rps_action:") for value in _callbacks(engine.get_keyboard(state, 91, 1002)))

    state = engine.apply_action(state, 2001, {"choice": "scissors"})
    # A resolved nonterminal round resets the hidden choices for the next one.
    # Seed a final round here to assert that choices appear only after resolve.
    state["players"]["1001"]["choice"] = "rock"
    state["players"]["2001"]["choice"] = "scissors"
    state["finished"] = True
    revealed = engine.render_for_group(state, -500, 1001)
    assert "🗿 سنگ" in revealed and "✂️ قیچی" in revealed
    assert "تیم اول" in revealed


def test_friendly_high_card_shared_keyboard_does_not_leak_first_draw():
    engine = HighCardEngine()
    state = _friendly_state(engine, _players())
    state = engine.apply_action(state, 1001, {"rank": 14, "suit": "♠️"})
    waiting = engine.render_for_group(state, -500, 2001)
    assert "تک / آس" not in waiting
    assert "کارت ثبت شده" in waiting
    assert all(value.startswith("hc_action:") for value in _callbacks(engine.get_keyboard(state, 92, 2001)))

    state = engine.apply_action(state, 2001, {"rank": 2, "suit": "♥️"})
    revealed = engine.render_for_group(state, -500, 1001)
    assert "تک / آس" in revealed and "۲" in revealed
    assert "تیم اول" in revealed


def test_friendly_tic_tac_toe_and_connect_four_show_both_team_perspectives():
    players = _players()
    ttt = _friendly_state(TicTacToeEngine(), players)
    ttt_text = TicTacToeEngine().render_for_group(ttt, -500, 2001)
    assert "اطلاعات بازی ناقص" not in ttt_text
    assert "تیم اول" in ttt_text and "تیم دوم" in ttt_text
    assert "بازیکن 1-1" in ttt_text and "بازیکن 2-1" in ttt_text

    connect_four = _friendly_state(ConnectFourEngine(), _players(2), mode="2v2")
    c4_text = ConnectFourEngine().render_for_group(connect_four, -500, 1001)
    assert "تیم اول" in c4_text and "تیم دوم" in c4_text
    for name in ("بازیکن 1-1", "بازیکن 1-2", "بازیکن 2-1", "بازیکن 2-2"):
        assert name in c4_text


def test_friendly_battleship_shared_board_contains_both_radar_histories():
    engine = BattleshipEngine()
    state = _friendly_state(engine, _players(2), mode="2v2")
    state["teams"]["A"]["shots_taken"]["0,0"] = "hit"
    state["teams"]["B"]["shots_taken"]["7,7"] = "miss"
    rendered = engine.render_for_group(state, -500, state["current_player_id"])
    assert "رادار شلیک‌های تیم اول" in rendered
    assert "رادار شلیک‌های تیم دوم" in rendered
    assert "خانه‌های سالم ناوگان تیم اول" in rendered
    assert "خانه‌های سالم ناوگان تیم دوم" in rendered
    assert "💥" in rendered and "🟦" in rendered
    # Ship-cell coordinates are never rendered by either radar.
    assert "cells" not in rendered


async def _create_friendly_rps_match(session, group, players):
    party = await PartyService.create_party(
        session, group.id, players[0].id, "rock_paper_scissors", "solo",
        match_type=MatchType.FRIENDLY,
    )
    party = await PartyService.join_party(
        session, party.id, players[1].id, team=2, group_id=group.id
    )
    match = await FriendlyMatchService.start_party(
        session, party.id, players[0].id, group_id=group.id
    )
    match.group1_message_id = 7654
    await session.commit()
    return party, match


def _callback(user, group_id, data):
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    callback.message = MagicMock(spec=Message)
    callback.message.chat = TelegramChat(id=group_id, type="supergroup", title="گروه")
    callback.message.message_id = 7654
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_friendly_action_is_public_to_match_participants_but_bound_to_owning_group(
    db_session, monkeypatch
):
    group = await create_test_group(db_session, "گروه بازی عمومی")
    other_group = await create_test_group(db_session, "گروه کپی دکمه")
    users = [await create_test_user(db_session, f"بازیکن اکشن {i}") for i in range(2)]
    _party, match = await _create_friendly_rps_match(db_session, group, users)
    await db_session.refresh(match)
    state = json.loads(match.game_state)

    monkeypatch.setattr(game_common, "safe_edit_screen", AsyncMock(return_value=7654))
    actor = users[1]
    good = _callback(actor, group.id, f"rps_action:{match.id}:paper")
    await process_game_action(good, AsyncMock(), match.id, {"choice": "paper"})
    good.answer.assert_awaited()

    updated = await db_session.scalar(select(Match).where(Match.id == match.id))
    await db_session.refresh(updated)
    after = json.loads(updated.game_state)
    assert after["players"][str(actor.id)]["locked"] is True
    assert after["players"][str(users[0].id)]["locked"] is False

    wrong_group = _callback(actor, other_group.id, f"rps_action:{match.id}:rock")
    await process_game_action(wrong_group, AsyncMock(), match.id, {"choice": "rock"})
    wrong_group.answer.assert_awaited()
    text = wrong_group.answer.await_args.args[0]
    assert "گروه ثبت‌شده" in text

    latest = await db_session.scalar(select(Match).where(Match.id == match.id))
    await db_session.refresh(latest)
    assert json.loads(latest.game_state) == after


@pytest.mark.asyncio
async def test_game_refresh_and_stale_action_reject_cross_chat_before_render(db_session, monkeypatch):
    group = await create_test_group(db_session, "گروه اصلی صفحه")
    other_group = await create_test_group(db_session, "گروه دکمه‌ی کپی‌شده")
    users = [await create_test_user(db_session, f"بازیکن صفحه {i}") for i in range(2)]
    _party, match = await _create_friendly_rps_match(db_session, group, users)
    match.state = "FINISHED"
    await db_session.commit()
    monkeypatch.setattr(game_common, "safe_edit_callback_message", AsyncMock(return_value=True))

    copied = _callback(users[0], other_group.id, f"game_refresh:{match.id}")
    await handle_game_refresh(copied, AsyncMock())
    copied.answer.assert_awaited()
    assert "ثبت‌شده" in copied.answer.await_args.args[0]
    game_common.safe_edit_callback_message.assert_not_awaited()

    stale = _callback(users[0], other_group.id, f"rps_action:{match.id}:rock")
    await process_game_action(stale, AsyncMock(), match.id, {"choice": "rock"})
    stale.answer.assert_awaited()
    assert "ثبت‌شده" in stale.answer.await_args.args[0]
    game_common.safe_edit_callback_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_competitive_acceptance_is_bound_to_participant_group(db_session):
    group_one = await create_test_group(db_session, "گروه پذیرش یک")
    group_two = await create_test_group(db_session, "گروه پذیرش دو")
    users = [await create_test_user(db_session, f"پذیرش {i}") for i in range(2)]
    party_one = await PartyService.create_party(
        db_session, group_one.id, users[0].id, "rock_paper_scissors", "solo"
    )
    party_two = await PartyService.create_party(
        db_session, group_two.id, users[1].id, "rock_paper_scissors", "solo"
    )
    await db_session.commit()
    await MatchmakingService.enqueue_party(db_session, party_one.id, users[0].id)
    await MatchmakingService.enqueue_party(db_session, party_two.id, users[1].id)
    match = await MatchmakingService.find_and_create_match(db_session, party_two.id)
    await db_session.commit()

    participant = next(player for player in match.players if player.user_id == users[0].id)
    own_group_id = participant.group_id
    other_group_id = group_two.id if own_group_id == group_one.id else group_one.id
    callback = _callback(users[0], other_group_id, f"accept_match:{match.id}")
    await handle_accept_match(callback, AsyncMock())
    callback.answer.assert_awaited()
    assert "گروه ثبت‌شده" in callback.answer.await_args.args[0]

    await db_session.refresh(match)
    assert match.group1_accepted is False
    assert match.group2_accepted is False
    assert all(not player.has_accepted for player in match.players)

    # The legitimate public participant still accepts from their own group.
    callback = _callback(users[0], own_group_id, f"accept_match:{match.id}")
    await handle_accept_match(callback, AsyncMock())
    await db_session.refresh(match)
    if participant.team == 1:
        assert match.group1_accepted is True
    else:
        assert match.group2_accepted is True
