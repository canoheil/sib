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
    # After both draw, match is NOT finished - players must still keep/swap
    mock_cb2.answer.assert_called_once()

    # User 1 keeps card
    mock_cb3 = MagicMock(spec=CallbackQuery)
    mock_cb3.data = f"hc_action:{match.id}:keep"
    mock_cb3.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb3.message = MagicMock(spec=Message)
    mock_cb3.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb3.answer = AsyncMock()

    await handle_high_card_action(mock_cb3, mock_bot)
    mock_cb3.answer.assert_called_once()

    # User 2 keeps card - round 1 resolves, match continues (best of 5)
    mock_cb4 = MagicMock(spec=CallbackQuery)
    mock_cb4.data = f"hc_action:{match.id}:keep"
    mock_cb4.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb4.message = MagicMock(spec=Message)
    mock_cb4.message.chat = TelegramChat(id=g2.id, type="supergroup", title=g2.title)
    mock_cb4.answer = AsyncMock()

    await handle_high_card_action(mock_cb4, mock_bot)
    # Round 1 resolved, match continues (not finished yet - best of 5)
    mock_cb4.answer.assert_called_once()

    # The handler commits through a separate session. End the test
    # session transaction so MySQL can observe that committed state.
    await db_session.rollback()
    await db_session.refresh(match)
    # After 1 round, match is still ACTIVE (best of 5, first to 3)
    assert match.state == MatchState.ACTIVE


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
    mock_cb_err = MagicMock(spec=CallbackQuery)
    mock_cb_err.data = f"ttt_action:{match.id}:0"
    mock_cb_err.from_user = TelegramUser(id=inactive_pid, is_bot=False, first_name="Inactive")
    mock_cb_err.answer = AsyncMock()

    await handle_ttt_action(mock_cb_err, mock_bot)
    assert "هنوز نوبت شما نشده است" in mock_cb_err.answer.call_args[0][0]

    # Active player moves
    mock_cb_act = MagicMock(spec=CallbackQuery)
    mock_cb_act.data = f"ttt_action:{match.id}:0"
    mock_cb_act.from_user = TelegramUser(id=active_pid, is_bot=False, first_name="Active")
    mock_cb_act.answer = AsyncMock()

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

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"c4_action:{match.id}:3"
    mock_cb.from_user = TelegramUser(id=active_pid, is_bot=False, first_name="Active")
    mock_cb.answer = AsyncMock()

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

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"bs_action:{match.id}:0:0"
    mock_cb.from_user = TelegramUser(id=active_pid, is_bot=False, first_name="Active")
    mock_cb.answer = AsyncMock()

    await handle_bs_action(mock_cb, mock_bot)
    assert "ثبت شد" in mock_cb.answer.call_args[0][0]

    await db_session.refresh(match)
    st = json.loads(match.game_state)
    active_team = st["players"][str(active_pid)].get("team_code", "A")
    assert "0,0" in st["teams"][active_team]["shots_taken"]
    assert st["current_player_id"] == inactive_pid
