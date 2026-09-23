"""Tests for admin authorization system."""

from unittest.mock import MagicMock
import pytest
from aiogram.types import Message, User as TelegramUser
from goje.admin.auth import is_admin, IsAdminFilter, admin_required
from goje.core.config import settings
from goje.core.errors import UnauthorizedAdminException


def test_is_admin_check():
    """Verify is_admin checks against settings.ADMIN_IDS."""
    settings.ADMIN_IDS = [123456, 789012]

    assert is_admin(123456) is True
    assert is_admin(789012) is True
    assert is_admin(999999) is False


@pytest.mark.asyncio
async def test_admin_filter_allows_admin():
    """Verify IsAdminFilter passes for authorized admins."""
    settings.ADMIN_IDS = [123456]
    admin_filter = IsAdminFilter()

    mock_message = MagicMock(spec=Message)
    mock_message.from_user = TelegramUser(id=123456, is_bot=False, first_name="Admin")

    result = await admin_filter(mock_message)
    assert result is True


@pytest.mark.asyncio
async def test_admin_filter_blocks_non_admin():
    """Verify IsAdminFilter rejects unauthorized users."""
    settings.ADMIN_IDS = [123456]
    admin_filter = IsAdminFilter()

    mock_message = MagicMock(spec=Message)
    mock_message.from_user = TelegramUser(id=999999, is_bot=False, first_name="NormalUser")

    result = await admin_filter(mock_message)
    assert result is False


@pytest.mark.asyncio
async def test_admin_required_decorator():
    """Verify admin_required decorator raises UnauthorizedAdminException for non-admins."""
    settings.ADMIN_IDS = [123456]

    @admin_required
    async def secret_admin_function(event):
        return "SUCCESS"

    mock_admin_msg = MagicMock(spec=Message)
    mock_admin_msg.from_user = TelegramUser(id=123456, is_bot=False, first_name="Admin")
    assert await secret_admin_function(mock_admin_msg) == "SUCCESS"

    mock_user_msg = MagicMock(spec=Message)
    mock_user_msg.from_user = TelegramUser(id=999999, is_bot=False, first_name="NormalUser")

    with pytest.raises(UnauthorizedAdminException):
        await secret_admin_function(mock_user_msg)
