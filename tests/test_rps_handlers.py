"""Tests for Rock Paper Scissors action callback handler."""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TelegramUser, Chat as TelegramChat
from sib.database.models import Match, MatchState
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.handlers.games.rps import handle_rps_action
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_rps_action_callback(db_session):
    """Clicking rock/paper/scissors records choice, updates message, and ends game when both choose."""
    g1 = await create_test_group(db_session, "تست هندلر گیم ۱")
    g2 = await create_test_group(db_session, "تست هندلر گیم ۲")
    u1 = await create_test_user(db_session, "بازیکن الف")
    u2 = await create_test_user(db_session, "بازیکن ب")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = RockPaperScissorsEngine()
    initial_state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(initial_state)
    await db_session.commit()

    # User 1 clicks Rock
    mock_cb1 = MagicMock(spec=CallbackQuery)
    mock_cb1.data = f"rps_action:{match.id}:rock"
    mock_cb1.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb1.message = MagicMock(spec=Message)
    mock_cb1.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb1.message.message_id = 111
    mock_cb1.message.edit_text = AsyncMock()
    mock_cb1.answer = AsyncMock()

    mock_bot = AsyncMock()
    await handle_rps_action(mock_cb1, mock_bot)

    mock_cb1.answer.assert_called_once()
    mock_bot.edit_message_text.assert_awaited_once()
    args1, _ = mock_bot.edit_message_text.call_args
    assert "انتخاب شما ثبت و قفل شد" in args1[0]

    # User 2 clicks Scissors: round 1 ends, match continues.
    mock_cb2 = MagicMock(spec=CallbackQuery)
    mock_cb2.data = f"rps_action:{match.id}:scissors"
    mock_cb2.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb2.message = MagicMock(spec=Message)
    mock_cb2.message.chat = TelegramChat(id=g2.id, type="supergroup", title=g2.title)
    mock_cb2.message.message_id = 222
    mock_cb2.message.edit_text = AsyncMock()
    mock_cb2.answer = AsyncMock()

    await handle_rps_action(mock_cb2, mock_bot)

    mock_cb2.answer.assert_called_once()
    args2 = mock_cb2.answer.call_args[0]
    assert "پایان رسید" not in args2[0]

    # User 1 starts round 2 and User 2 answers.
    mock_cb3 = MagicMock(spec=CallbackQuery)
    mock_cb3.data = f"rps_action:{match.id}:rock"
    mock_cb3.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb3.message = MagicMock(spec=Message)
    mock_cb3.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb3.message.message_id = 111
    mock_cb3.message.edit_text = AsyncMock()
    mock_cb3.answer = AsyncMock()

    await handle_rps_action(mock_cb3, mock_bot)

    mock_cb4 = MagicMock(spec=CallbackQuery)
    mock_cb4.data = f"rps_action:{match.id}:scissors"
    mock_cb4.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb4.message = MagicMock(spec=Message)
    mock_cb4.message.chat = TelegramChat(id=g2.id, type="supergroup", title=g2.title)
    mock_cb4.message.message_id = 222
    mock_cb4.message.edit_text = AsyncMock()
    mock_cb4.answer = AsyncMock()

    await handle_rps_action(mock_cb4, mock_bot)

    mock_cb4.answer.assert_called_once()
    args4 = mock_cb4.answer.call_args[0]
    assert "پایان رسید" in args4[0]
