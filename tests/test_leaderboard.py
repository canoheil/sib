"""Tests for Global Leaderboard, Group Stats, and Player Stats."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import Message, CallbackQuery, User as TelegramUser, Chat as TelegramChat
from sqlalchemy import select
from sib.database.models import Group, Rating, PlayerStats
from sib.services.leaderboard import LeaderboardService
from sib.handlers.leaderboard import (
    cmd_leaderboard,
    callback_show_leaderboard,
    callback_group_rating_stats,
    cmd_stats,
)
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_global_leaderboard_sorting(db_session):
    """Leaderboard must sort groups by Rating DESC, then wins DESC."""
    g1 = await create_test_group(db_session, "تیم طلایی")
    g2 = await create_test_group(db_session, "تیم نقره‌ای")
    g3 = await create_test_group(db_session, "تیم برنزی")

    # Set distinct ratings
    r1 = await db_session.scalar(select(Rating).where(Rating.group_id == g1.id))
    r1.rating = 1750.0
    r1.wins = 15

    r2 = await db_session.scalar(select(Rating).where(Rating.group_id == g2.id))
    r2.rating = 1620.0
    r2.wins = 8

    r3 = await db_session.scalar(select(Rating).where(Rating.group_id == g3.id))
    r3.rating = 1490.0
    r3.wins = 2

    await db_session.commit()

    top_groups = await LeaderboardService.get_top_groups(db_session, limit=10)

    assert len(top_groups) >= 3
    # First place must be g1
    assert top_groups[0][0] == 1  # Rank 1
    assert top_groups[0][1].id == g1.id
    assert top_groups[0][2].rating == 1750.0

    # Second place must be g2
    assert top_groups[1][0] == 2  # Rank 2
    assert top_groups[1][1].id == g2.id

    # Third place must be g3
    assert top_groups[2][0] == 3  # Rank 3
    assert top_groups[2][1].id == g3.id


@pytest.mark.asyncio
async def test_group_rank_and_stats_calculation(db_session):
    """Group rank calculation matches position in global rating hierarchy."""
    g_high = await create_test_group(db_session, "تیم صدرنشین")
    g_mid = await create_test_group(db_session, "تیم میانه")

    r_high = await db_session.scalar(select(Rating).where(Rating.group_id == g_high.id))
    r_high.rating = 1800.0

    r_mid = await db_session.scalar(select(Rating).where(Rating.group_id == g_mid.id))
    r_mid.rating = 1600.0
    r_mid.matches_played = 10
    r_mid.wins = 6
    r_mid.losses = 3
    r_mid.draws = 1

    await db_session.commit()

    stats = await LeaderboardService.get_group_stats_and_rank(db_session, g_mid.id)
    assert stats is not None
    assert stats["rank"] >= 2
    assert stats["matches_played"] == 10
    assert stats["wins"] == 6
    assert stats["win_rate"] == 60.0


@pytest.mark.asyncio
async def test_player_stats_query(db_session):
    """Verify player stats query returns correct counts and win rate."""
    user = await create_test_user(db_session, "بازیکن آمار")

    p_stat = PlayerStats(
        user_id=user.id,
        matches_played=20,
        wins=14,
        losses=4,
        draws=2,
    )
    db_session.add(p_stat)
    await db_session.commit()

    stats = await LeaderboardService.get_player_stats(db_session, user.id)
    assert stats["matches_played"] == 20
    assert stats["wins"] == 14
    assert stats["losses"] == 4
    assert stats["draws"] == 2
    assert stats["win_rate"] == 70.0


@pytest.mark.asyncio
async def test_cmd_leaderboard_handler(db_session):
    """Calling /leaderboard returns Persian formatted leaderboard with medals."""
    g = await create_test_group(db_session, "گروه تست لیدربورد")
    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=g.id, type="supergroup", title=g.title)
    mock_msg.reply = AsyncMock()

    await cmd_leaderboard(mock_msg)

    mock_msg.reply.assert_called_once()
    args, kwargs = mock_msg.reply.call_args
    assert "برترین گروه‌های سیب" in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_cmd_stats_in_private_chat(db_session):
    """Calling /stats in private chat shows player's personal participation record."""
    user = await create_test_user(db_session, "فرد در پیوی")

    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=user.id, type="private")
    mock_msg.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    mock_msg.reply = AsyncMock()

    await cmd_stats(mock_msg)

    mock_msg.reply.assert_called_once()
    args, kwargs = mock_msg.reply.call_args
    assert "کارنامه بازیکن" in args[0]
    assert user.first_name in args[0]
