"""Tests for Global Leaderboard, Group Stats, and Player Stats (win-based)."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import Message, CallbackQuery, User as TelegramUser, Chat as TelegramChat
from sqlalchemy import select
from sib.database.models import Group, GroupStats, PlayerStats
from sib.services.leaderboard import LeaderboardService
from sib.handlers.leaderboard import (
    cmd_leaderboard,
    callback_show_leaderboard,
    callback_group_rating_stats,
    cmd_stats,
)
from sib.tests.test_parties import create_test_user, create_test_group


async def _set_stats(db_session, group_id, wins=0, losses=0, draws=0, matches_played=0):
    stats = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == group_id))
    stats.wins = wins
    stats.losses = losses
    stats.draws = draws
    stats.matches_played = matches_played
    await db_session.commit()
    return stats


@pytest.mark.asyncio
async def test_global_leaderboard_sorting_by_wins(db_session):
    """Leaderboard must sort groups by wins DESC (losses never subtract wins)."""
    g1 = await create_test_group(db_session, "تیم طلایی")
    g2 = await create_test_group(db_session, "تیم نقره‌ای")
    g3 = await create_test_group(db_session, "تیم برنزی")

    await _set_stats(db_session, g1.id, wins=15, losses=2, draws=1, matches_played=18)
    await _set_stats(db_session, g2.id, wins=8, losses=10, draws=0, matches_played=18)
    await _set_stats(db_session, g3.id, wins=2, losses=1, draws=0, matches_played=3)

    top_groups = await LeaderboardService.get_top_groups(db_session, limit=10)

    assert len(top_groups) >= 3
    assert top_groups[0][0] == 1
    assert top_groups[0][1].id == g1.id
    assert top_groups[0][2].wins == 15

    assert top_groups[1][0] == 2
    assert top_groups[1][1].id == g2.id

    assert top_groups[2][0] == 3
    assert top_groups[2][1].id == g3.id


@pytest.mark.asyncio
async def test_leaderboard_loss_does_not_subtract_wins(db_session):
    """A group with more losses but more wins stays above."""
    g1 = await create_test_group(db_session, "تیم پربرد")
    g2 = await create_test_group(db_session, "تیم کم‌برد")

    await _set_stats(db_session, g1.id, wins=5, losses=20, draws=0, matches_played=25)
    await _set_stats(db_session, g2.id, wins=4, losses=0, draws=0, matches_played=4)

    top_groups = await LeaderboardService.get_top_groups(db_session, limit=10)
    ids_in_order = [g.id for _, g, _ in top_groups]
    assert ids_in_order.index(g1.id) < ids_in_order.index(g2.id)


@pytest.mark.asyncio
async def test_leaderboard_tie_breakers_are_deterministic(db_session):
    """Equal wins -> win rate, then matches played, then group id."""
    g1 = await create_test_group(db_session, "گروه الف")
    g2 = await create_test_group(db_session, "گروه ب")
    g3 = await create_test_group(db_session, "گروه ج")

    # Same wins; g1 has better win rate (fewer matches).
    await _set_stats(db_session, g1.id, wins=5, losses=0, draws=0, matches_played=5)
    await _set_stats(db_session, g2.id, wins=5, losses=5, draws=0, matches_played=10)
    # Same wins AND same rate AND same matches -> stable id ordering.
    await _set_stats(db_session, g3.id, wins=5, losses=0, draws=0, matches_played=5)

    top_groups = await LeaderboardService.get_top_groups(db_session, limit=10)
    ids_in_order = [g.id for _, g, _ in top_groups]
    i1, i2, i3 = (ids_in_order.index(g1.id), ids_in_order.index(g2.id), ids_in_order.index(g3.id))
    assert i2 > i1  # worse win rate ranks lower
    # identical stats: lower group id first
    assert (i1 < i3) if g1.id < g3.id else (i3 < i1)


@pytest.mark.asyncio
async def test_group_rank_and_stats_calculation(db_session):
    """Group rank calculation matches position in win hierarchy."""
    g_high = await create_test_group(db_session, "تیم صدرنشین")
    g_mid = await create_test_group(db_session, "تیم میانه")

    await _set_stats(db_session, g_high.id, wins=20, matches_played=20)
    await _set_stats(db_session, g_mid.id, wins=6, losses=3, draws=1, matches_played=10)

    stats = await LeaderboardService.get_group_stats_and_rank(db_session, g_mid.id)
    assert stats is not None
    assert stats["rank"] >= 2
    assert stats["matches_played"] == 10
    assert stats["wins"] == 6
    assert stats["win_rate"] == 60.0
    assert "rating" not in stats


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
    assert "ریتینگ" not in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_main_leaderboard_button_still_exists(db_session):
    """AB1 preserves /leaderboard routing, rendering and the main-menu path."""
    from sib.handlers.leaderboard import router
    from sib.keyboards.common import get_group_welcome_keyboard
    from sib.keyboards.leaderboard import get_group_stats_keyboard

    main_buttons = {
        button.callback_data: button.text
        for row in get_group_welcome_keyboard().inline_keyboard for button in row
    }
    assert main_buttons["group_rating_stats"] == "🏆 رتبه و آمار گروه"
    stats_buttons = {
        button.callback_data: button.text
        for row in get_group_stats_keyboard().inline_keyboard for button in row
    }
    assert stats_buttons["show_leaderboard"] == "🏆 برترین گروه‌ها"

    message = Message(
        message_id=1,
        date=0,
        chat=TelegramChat(id=-123, type="supergroup", title="گروه تست"),
        from_user=TelegramUser(id=123, is_bot=False, first_name="بازیکن"),
        text="/leaderboard",
    )
    entry = next(h for h in router.message.handlers if h.callback is cmd_leaderboard)
    matched, _ = await entry.check(message, bot=MagicMock())
    assert matched, "/leaderboard must still route to the main leaderboard handler"

    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = message.chat
    mock_msg.reply = AsyncMock()
    await entry.call(mock_msg)

    mock_msg.reply.assert_called_once()
    args, kwargs = mock_msg.reply.call_args
    assert "برترین گروه‌های سیب" in args[0]
    callbacks = {
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard for button in row
    }
    assert "show_leaderboard" in callbacks
    assert "ab_board" not in callbacks


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
    assert "ریتینگ" not in args[0]
