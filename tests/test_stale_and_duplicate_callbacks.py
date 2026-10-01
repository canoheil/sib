"""Tests for stale callback queries, duplicate clicks, and unauthorized button presses."""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TelegramUser, Chat as TelegramChat

from sib.database.models import Match, MatchState
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.handlers.games.rps import handle_rps_action
from sib.handlers.games.ads import handle_noop_callback
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_non_existent_match_callback():
    """Clicking button for a non-existent match ID gracefully returns error alert."""
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = "rps_action:99999999:rock"
    mock_cb.from_user = TelegramUser(id=123, is_bot=False, first_name="Stranger")
    mock_cb.answer = AsyncMock()

    mock_bot = AsyncMock()
    await handle_rps_action(mock_cb, mock_bot)

    mock_cb.answer.assert_called_once()
    assert "مسابقه پیدا نشد" in mock_cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_stale_callback_on_finished_or_cancelled_match(db_session):
    """Clicking button on an already finished or cancelled match returns friendly stale warning."""
    g1 = await create_test_group(db_session, "گروه تست باطله ۱")
    g2 = await create_test_group(db_session, "گروه تست باطله ۲")
    u1 = await create_test_user(db_session, "کاربر ۱")
    u2 = await create_test_user(db_session, "کاربر ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    match.state = MatchState.FINISHED
    await db_session.commit()

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"rps_action:{match.id}:rock"
    mock_cb.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    participant = next(p for p in match.players if p.user_id == u1.id)
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.chat = TelegramChat(id=participant.group_id, type="supergroup", title="گروه تست")
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()

    mock_bot = AsyncMock()
    await handle_rps_action(mock_cb, mock_bot)

    mock_cb.answer.assert_called_once()
    assert "دیگر در جریان نیست" in mock_cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_unauthorized_user_gameplay_action_rejected(db_session):
    """An outsider in the group clicking a gameplay button is blocked with friendly alert."""
    g1 = await create_test_group(db_session, "گروه نفوذ ۱")
    g2 = await create_test_group(db_session, "گروه نفوذ ۲")
    u1 = await create_test_user(db_session, "کاربر ۱")
    u2 = await create_test_user(db_session, "کاربر ۲")
    outsider = await create_test_user(db_session, "فرد ناشناس")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = RockPaperScissorsEngine()
    match.game_state = json.dumps(engine.create_initial_state(match.players, match.mode))
    await db_session.commit()

    # Outsider attempts to play rock
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"rps_action:{match.id}:rock"
    mock_cb.from_user = TelegramUser(id=outsider.id, is_bot=False, first_name=outsider.first_name)
    mock_cb.answer = AsyncMock()

    mock_bot = AsyncMock()
    await handle_rps_action(mock_cb, mock_bot)

    mock_cb.answer.assert_called_once()
    assert "شما بازیکن این مسابقه نیستید" in mock_cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_duplicate_action_click_rejected_idempotently(db_session):
    """Clicking an action twice rapidly: first locks in, second is rejected cleanly."""
    g1 = await create_test_group(db_session, "گروه دابل‌کلیک ۱")
    g2 = await create_test_group(db_session, "گروه دابل‌کلیک ۲")
    u1 = await create_test_user(db_session, "کاربر ۱")
    u2 = await create_test_user(db_session, "کاربر ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = RockPaperScissorsEngine()
    match.game_state = json.dumps(engine.create_initial_state(match.players, match.mode))
    await db_session.commit()

    mock_bot = AsyncMock()

    # First click: Rock -> Valid
    mock_cb1 = MagicMock(spec=CallbackQuery)
    mock_cb1.data = f"rps_action:{match.id}:rock"
    mock_cb1.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb1.message = MagicMock(spec=Message)
    mock_cb1.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb1.message.edit_text = AsyncMock()
    mock_cb1.answer = AsyncMock()
    await handle_rps_action(mock_cb1, mock_bot)
    assert "ثبت شد" in mock_cb1.answer.call_args[0][0]

    # Second click: Rock (or Scissors) -> Rejected because choice already locked
    mock_cb2 = MagicMock(spec=CallbackQuery)
    mock_cb2.data = f"rps_action:{match.id}:rock"
    mock_cb2.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb2.message = MagicMock(spec=Message)
    mock_cb2.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb2.message.edit_text = AsyncMock()
    mock_cb2.answer = AsyncMock()
    await handle_rps_action(mock_cb2, mock_bot)
    assert "قفل کرده‌اید" in mock_cb2.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_noop_callback_answered_silently():
    """Clicking disabled/noop buttons answers callback query silently without error."""
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = "noop"
    mock_cb.answer = AsyncMock()

    await handle_noop_callback(mock_cb)
    mock_cb.answer.assert_called_once_with()
