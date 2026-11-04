"""Tests for admin authorization system."""

import logging
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TelegramUser
from sib.admin.auth import (
    NO_ADMIN_ACCESS,
    is_admin,
    IsAdminFilter,
    admin_required,
)
from sib.core.config import settings
from sib.core.errors import UnauthorizedAdminException


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


# ---------------------------------------------------------------------------
# IsAdminFilter: silent for wrongly routed callbacks, loud for real attempts
# ---------------------------------------------------------------------------

def _callback(data: str, user_id: int = 999999):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="NormalUser")
    cb.data = data
    cb.answer = AsyncMock()
    return cb


def _message(text: str, user_id: int = 999999):
    msg = MagicMock(spec=Message)
    msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name="NormalUser")
    msg.text = text
    msg.caption = None
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_admin_filter_stays_silent_for_friendship_callbacks(
    monkeypatch, caplog
):
    """A friendship button («✅ قبول میکنم» -> ``friend:accept:…``) routed
    through an admin router is NOT an admin attempt: refused silently, with
    no warning log and no alert."""
    monkeypatch.setattr(settings, "ADMIN_IDS", [123456])
    caplog.set_level(logging.WARNING)

    for data in ("friend:accept:123", "friend:reject:123"):
        cb = _callback(data)
        assert await IsAdminFilter()(cb) is False
        cb.answer.assert_not_awaited()

    assert not [r for r in caplog.records if "Unauthorized admin" in r.getMessage()]


@pytest.mark.asyncio
async def test_admin_filter_stays_silent_for_plain_messages(monkeypatch, caplog):
    """An ordinary message passing an admin router's filter is not an attempt."""
    monkeypatch.setattr(settings, "ADMIN_IDS", [123456])
    caplog.set_level(logging.WARNING)

    msg = _message("سلام")
    assert await IsAdminFilter()(msg) is False
    msg.answer.assert_not_awaited()
    assert not [r for r in caplog.records if "Unauthorized admin" in r.getMessage()]


@pytest.mark.asyncio
async def test_admin_filter_logs_callback_data_and_alerts_real_admin_button(
    monkeypatch, caplog
):
    """A REAL admin callback pressed by a non-admin logs the callback_data
    and answers with the no-access alert."""
    monkeypatch.setattr(settings, "ADMIN_IDS", [123456])
    caplog.set_level(logging.WARNING)

    cb = _callback("admin_main")
    assert await IsAdminFilter()(cb) is False

    warnings = [r for r in caplog.records if "Unauthorized admin" in r.getMessage()]
    assert len(warnings) == 1
    assert "999999" in warnings[0].getMessage()
    assert "admin_main" in warnings[0].getMessage()  # the actual callback data
    cb.answer.assert_awaited_once_with(NO_ADMIN_ACCESS, show_alert=True)


@pytest.mark.asyncio
async def test_admin_filter_logs_command_and_replies_real_admin_command(
    monkeypatch, caplog
):
    """A REAL admin command typed by a non-admin logs the command text and
    gets the «⛔ شما دسترسی ادمین ندارید!» message."""
    monkeypatch.setattr(settings, "ADMIN_IDS", [123456])
    caplog.set_level(logging.WARNING)

    msg = _message("/admin")
    assert await IsAdminFilter()(msg) is False

    warnings = [r for r in caplog.records if "Unauthorized admin" in r.getMessage()]
    assert len(warnings) == 1
    assert "999999" in warnings[0].getMessage()
    assert "/admin" in warnings[0].getMessage()  # the actual command text
    msg.answer.assert_awaited_once_with(NO_ADMIN_ACCESS)


@pytest.mark.asyncio
async def test_admin_filter_ignores_non_admin_command_silently(monkeypatch, caplog):
    """A non-admin command (e.g. /start) is not an admin attempt: no log."""
    monkeypatch.setattr(settings, "ADMIN_IDS", [123456])
    caplog.set_level(logging.WARNING)

    msg = _message("/start")
    assert await IsAdminFilter()(msg) is False
    msg.answer.assert_not_awaited()
    assert not [r for r in caplog.records if "Unauthorized admin" in r.getMessage()]
