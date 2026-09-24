"""Tests for official channel membership gate."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.exceptions import TelegramBadRequest
from sib.core.config import settings
from sib.database.models import SponsorChannel, ChannelType
from sib.services.membership.checker import MembershipService


@pytest.mark.asyncio
async def test_membership_verified_for_active_member(db_session):
    """User who is a member should pass the membership gate."""
    mock_bot = AsyncMock()
    mock_member = MagicMock()
    mock_member.status = "member"
    mock_bot.get_chat_member.return_value = mock_member

    user_id = 12345678
    is_allowed, missing = await MembershipService.check_user_membership(mock_bot, user_id, db_session)

    assert is_allowed is True
    assert missing is None
    mock_bot.get_chat_member.assert_called_once()


@pytest.mark.asyncio
async def test_membership_rejected_for_left_user(db_session):
    """User who has left the channel should be blocked by the membership gate."""
    mock_bot = AsyncMock()
    mock_member = MagicMock()
    mock_member.status = "left"
    mock_bot.get_chat_member.return_value = mock_member

    user_id = 99887766
    is_allowed, missing = await MembershipService.check_user_membership(mock_bot, user_id, db_session)

    assert is_allowed is False
    assert missing is not None
    assert missing.channel_id == "@sib_official"


@pytest.mark.asyncio
async def test_membership_bypass_when_disabled():
    """When membership checking is disabled, all users should pass."""
    settings.MEMBERSHIP_CHECK_ENABLED = False
    mock_bot = AsyncMock()

    is_allowed, missing = await MembershipService.check_user_membership(mock_bot, 12345)
    assert is_allowed is True
    assert missing is None
    mock_bot.get_chat_member.assert_not_called()

    # Re-enable
    settings.MEMBERSHIP_CHECK_ENABLED = True


@pytest.mark.asyncio
async def test_membership_telegram_error_handled_gracefully(db_session):
    """If Telegram returns error (e.g. ChatNotFound), gate rejects safely without crashing."""
    mock_bot = AsyncMock()
    mock_bot.get_chat_member.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Chat not found",
    )

    user_id = 55555
    is_allowed, missing = await MembershipService.check_user_membership(mock_bot, user_id, db_session)

    assert is_allowed is False
    assert missing is not None
