"""Integration tests for all new game action handlers."""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TelegramUser, Chat as TelegramChat
from sib.database.models import Match, MatchState
from sib.games.registry import get_game_engine
from sib.handlers.games.high_card import handle_high_card_action
from sib.handlers.games.tic_tac_toe import handle_ttt_action
from sib.handlers.games.connect_four import handle_c4_action
from sib.handlers.games.battleship import handle_bs_action
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_user, create_test_group


def _game_callback(user_id, group_id, data, first_name="بازیکن"):
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = TelegramUser(id=user_id, is_bot=False, first_name=first_name)
    callback.message = MagicMock(spec=Message)
    callback.message.chat = TelegramChat(id=group_id, type="supergroup", title="گروه بازی")
    callback.message.message_id = 777
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_high_card_handler_flow(db_session):
    g1 = await create_test_group(db_session, "گروه های‌کارت ۱")
    g2 = await create_test_group(db_session, "گروه های‌کارت ۲")
    u1 = await create_test_user(db_session, "کاربر ۱")
    u2 = await create_test_user(db_session, "کاربر ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "high_card", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "high_card", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = get_game_engine("high_card")
    state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(state)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await db_session.commit()

    mock_bot = AsyncMock()

    # User 1 draws card
    mock_cb1 = MagicMock(spec=CallbackQuery)
    mock_cb1.data = f"hc_action:{match.id}:draw"
    mock_cb1.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb1.message = MagicMock(spec=Message)
    mock_cb1.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb1.answer = AsyncMock()

    await handle_high_card_action(mock_cb1, mock_bot)
    mock_cb1.answer.assert_called_once()
    assert mock_bot.edit_message_text.called

    # Regression: simultaneous games must keep the shared deadline
    # after the first player acts, so the second player can time out.
    await db_session.refresh(match)
    assert match.turn_deadline is not None

    # User 2 draws card
    mock_cb2 = MagicMock(spec=CallbackQuery)
    mock_cb2.data = f"hc_action:{match.id}:draw"
    mock_cb2.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb2.message = MagicMock(spec=Message)
    mock_cb2.message.chat = TelegramChat(id=g2.id, type="supergroup", title=g2.title)
    mock_cb2.answer = AsyncMock()

    await handle_high_card_action(mock_cb2, mock_bot)
    assert "پایان رسید" in mock_cb2.answer.call_args[0][0]

    # The handler commits through a separate session. End the test
    # session transaction so MySQL can observe that committed state.
    await db_session.rollback()
    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED


@pytest.mark.asyncio
async def test_tic_tac_toe_handler_flow(db_session):
    g1 = await create_test_group(db_session, "گروه دوز ۱")
    g2 = await create_test_group(db_session, "گروه دوز ۲")
    u1 = await create_test_user(db_session, "کاربر دوز ۱")
    u2 = await create_test_user(db_session, "کاربر دوز ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "tic_tac_toe", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "tic_tac_toe", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = get_game_engine("tic_tac_toe")
    state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(state)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await db_session.commit()

    active_pid = state["current_player_id"]
    inactive_pid = u2.id if active_pid == u1.id else u1.id
    mock_bot = AsyncMock()

    # Inactive player attempts to move out of turn
    inactive_group_id = next(p.group_id for p in match.players if p.user_id == inactive_pid)
    mock_cb_err = _game_callback(inactive_pid, inactive_group_id, f"ttt_action:{match.id}:0", "Inactive")

    await handle_ttt_action(mock_cb_err, mock_bot)
    assert "هنوز نوبت شما نشده است" in mock_cb_err.answer.call_args[0][0]

    # Active player moves
    active_group_id = next(p.group_id for p in match.players if p.user_id == active_pid)
    mock_cb_act = _game_callback(active_pid, active_group_id, f"ttt_action:{match.id}:0", "Active")

    await handle_ttt_action(mock_cb_act, mock_bot)
    assert "ثبت شد" in mock_cb_act.answer.call_args[0][0]

    # Now verify state in database
    await db_session.refresh(match)
    updated_state = json.loads(match.game_state)
    assert updated_state["board"][0] == "X"
    assert updated_state["current_player_id"] == inactive_pid


@pytest.mark.asyncio
async def test_connect_four_handler_flow(db_session):
    g1 = await create_test_group(db_session, "گروه چهارخط ۱")
    g2 = await create_test_group(db_session, "گروه چهارخط ۲")
    u1 = await create_test_user(db_session, "کاربر چهارخط ۱")
    u2 = await create_test_user(db_session, "کاربر چهارخط ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "connect_four", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "connect_four", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = get_game_engine("connect_four")
    state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(state)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await db_session.commit()

    mock_bot = AsyncMock()

    # Active player drops piece in col 3
    active_pid = state["current_player_id"]
    inactive_pid = u2.id if active_pid == u1.id else u1.id

    active_group_id = next(p.group_id for p in match.players if p.user_id == active_pid)
    mock_cb = _game_callback(active_pid, active_group_id, f"c4_action:{match.id}:3", "Active")

    await handle_c4_action(mock_cb, mock_bot)
    assert "ثبت شد" in mock_cb.answer.call_args[0][0]

    await db_session.refresh(match)
    st = json.loads(match.game_state)
    assert st["board"][5][3] is not None
    assert st["current_player_id"] == inactive_pid


@pytest.mark.asyncio
async def test_battleship_handler_flow(db_session):
    g1 = await create_test_group(db_session, "گروه جنگ‌کشتی ۱")
    g2 = await create_test_group(db_session, "گروه جنگ‌کشتی ۲")
    u1 = await create_test_user(db_session, "کاربر جنگ ۱")
    u2 = await create_test_user(db_session, "کاربر جنگ ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "battleship", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "battleship", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = get_game_engine("battleship")
    state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(state)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await db_session.commit()

    mock_bot = AsyncMock()

    # Active player fires at (0, 0)
    active_pid = state["current_player_id"]
    inactive_pid = u2.id if active_pid == u1.id else u1.id

    active_group_id = next(p.group_id for p in match.players if p.user_id == active_pid)
    mock_cb = _game_callback(active_pid, active_group_id, f"bs_action:{match.id}:0:0", "Active")

    await handle_bs_action(mock_cb, mock_bot)
    assert "ثبت شد" in mock_cb.answer.call_args[0][0]

    await db_session.refresh(match)
    st = json.loads(match.game_state)
    active_team = st["players"][str(active_pid)].get("team_code", "A")
    assert "0,0" in st["teams"][active_team]["shots_taken"]
    assert st["current_player_id"] == inactive_pid
