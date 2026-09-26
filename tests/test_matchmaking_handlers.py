"""Tests for Matchmaking Telegram Handlers, callbacks, and Privacy rendering."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TelegramUser, Chat as TelegramChat
from sib.database.models import MatchState
from sib.handlers.matchmaking.handlers import (
    handle_find_match,
    handle_cancel_matchmaking,
    handle_accept_match,
    handle_reject_match,
)
from sib.handlers.matchmaking.renderer import render_match_found_for_group
from sib.matchmaking.matcher import MatchmakingService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_solo_privacy_rendering(db_session):
    """Solo mode: Show opponent display name only, do NOT expose group title or numeric Telegram ID."""
    g1 = await create_test_group(db_session, "گروه مخفی ۱")
    g2 = await create_test_group(db_session, "گروه محرمانه ۲")
    u1 = await create_test_user(db_session, "امیرعلی")
    u2 = await create_test_user(db_session, "سحر")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await db_session.commit()

    # Render for Group 1
    rendered_g1 = render_match_found_for_group(match, g1.id)

    # Must contain opponent display name
    assert "سحر" in rendered_g1
    assert "امیرعلی" in rendered_g1
    # Must NOT contain opponent group name
    assert "گروه محرمانه ۲" not in rendered_g1
    # Must NOT contain numeric ID
    assert str(u2.id) not in rendered_g1


@pytest.mark.asyncio
async def test_team_transparency_rendering(db_session):
    """Team mode: Show group name, team members, and group ratings."""
    g1 = await create_test_group(db_session, "ببرهای مازندران")
    g2 = await create_test_group(db_session, "شیرهای البرز")
    u1 = await create_test_user(db_session, "ببر ۱")
    u2 = await create_test_user(db_session, "شیر ۱")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "connect_four", "2v2")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "connect_four", "2v2")
    # For testing, fill party 1 and party 2
    u1_b = await create_test_user(db_session, "ببر ۲")
    u2_b = await create_test_user(db_session, "شیر ۲")
    await PartyService.join_party(db_session, p1.id, u1_b.id)
    await PartyService.join_party(db_session, p2.id, u2_b.id)
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await db_session.commit()

    rendered_g1 = render_match_found_for_group(match, g1.id)

    # Must show both group titles and rosters
    assert "ببرهای مازندران" in rendered_g1
    assert "شیرهای البرز" in rendered_g1
    assert "ببر ۱" in rendered_g1
    assert "ببر ۲" in rendered_g1
    assert "شیر ۱" in rendered_g1
    assert "شیر ۲" in rendered_g1
    assert "برد" in rendered_g1


@pytest.mark.asyncio
async def test_handle_accept_match_handler(db_session):
    """Accept callback updates message."""
    g1 = await create_test_group(db_session, "گروه تایید ۱")
    g2 = await create_test_group(db_session, "گروه تایید ۲")
    u1 = await create_test_user(db_session, "بازیکن ۱")
    u2 = await create_test_user(db_session, "بازیکن ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await db_session.commit()

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"accept_match:{match.id}"
    mock_cb.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.chat = TelegramChat(id=g1.id, type="supergroup", title=g1.title)
    mock_cb.message.edit_reply_markup = AsyncMock()
    mock_cb.answer = AsyncMock()

    mock_bot = AsyncMock()
    await handle_accept_match(mock_cb, mock_bot)

    mock_cb.answer.assert_called_once()
    mock_cb.message.edit_reply_markup.assert_called_once()


@pytest.mark.asyncio
async def test_handle_accept_match_handler_second_accept_starts_game_without_lazy_load_error(db_session):
    """Second-side acceptance must eagerly load MatchPlayer.user before game initialization."""
    g1 = await create_test_group(db_session, "گروه شروع ۱")
    g2 = await create_test_group(db_session, "گروه شروع ۲")
    u1 = await create_test_user(db_session, "بازیکن شروع ۱")
    u2 = await create_test_user(db_session, "بازیکن شروع ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await db_session.commit()

    async def make_callback(user, group):
        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"accept_match:{match.id}"
        cb.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
        cb.message = MagicMock(spec=Message)
        cb.message.chat = TelegramChat(id=group.id, type="supergroup", title=group.title)
        cb.message.edit_reply_markup = AsyncMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    first_cb = await make_callback(u1, g1)
    second_cb = await make_callback(u2, g2)
    mock_bot = AsyncMock()

    await handle_accept_match(first_cb, mock_bot)
    await handle_accept_match(second_cb, mock_bot)

    second_cb.answer.assert_called_once()
    second_cb.message.edit_reply_markup.assert_not_called()

    await db_session.refresh(match)
    assert match.state == "ACTIVE"
    assert match.turn_deadline is not None
    assert match.turn_deadline > match.started_at
