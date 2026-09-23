"""Tests for bot handlers."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import Message, CallbackQuery, User as TelegramUser, Chat as TelegramChat
from goje.core.config import settings
from goje.handlers.start import handle_start, handle_check_membership, handle_help_menu
from goje.handlers.admin import handle_admin_command, handle_admin_dashboard


@pytest.mark.asyncio
async def test_start_private_chat_not_member(db_session):
    """User who is not a member receives the Persian membership gate message."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=111222, type="private")
    mock_msg.from_user = TelegramUser(id=111222, is_bot=False, first_name="رضا")
    mock_msg.answer = AsyncMock()

    mock_bot = AsyncMock()
    # Mock membership check returning not member
    with patch("goje.services.membership.checker.MembershipService.check_user_membership") as mock_check:
        mock_check.return_value = (False, None)
        await handle_start(mock_msg, mock_bot)

    mock_msg.answer.assert_called_once()
    args, kwargs = mock_msg.answer.call_args
    assert "عضو کانال رسمی شو" in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_start_private_chat_already_member(db_session):
    """User who is already a member receives the Persian welcome message and options."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=333444, type="private")
    mock_msg.from_user = TelegramUser(id=333444, is_bot=False, first_name="سارا")
    mock_msg.answer = AsyncMock()

    mock_bot = AsyncMock()
    with patch("goje.services.membership.checker.MembershipService.check_user_membership") as mock_check:
        mock_check.return_value = (True, None)
        await handle_start(mock_msg, mock_bot)

    mock_msg.answer.assert_called_once()
    args, kwargs = mock_msg.answer.call_args
    assert "سارا عزیز! من گوجه‌ام!" in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_start_group_chat_registers_group_and_rating(db_session):
    """Adding bot or calling /start in group registers group and sends group menu."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=-1009988776655, type="supergroup", title="گروه بر و بچ گوجه")
    mock_msg.from_user = TelegramUser(id=777, is_bot=False, first_name="کیان")
    mock_msg.reply = AsyncMock()

    mock_bot = AsyncMock()
    await handle_start(mock_msg, mock_bot)

    mock_msg.reply.assert_called_once()
    args, kwargs = mock_msg.reply.call_args
    assert "گروه شما ثبت شد" in args[0]
    assert "1500" in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_check_membership_callback(db_session):
    """Clicking check membership button confirms membership and edits message."""
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.from_user = TelegramUser(id=444555, is_bot=False, first_name="مهدی")
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.edit_text = AsyncMock()

    mock_bot = AsyncMock()
    with patch("goje.services.membership.checker.MembershipService.check_user_membership") as mock_check:
        mock_check.return_value = (True, None)
        await handle_check_membership(mock_cb, mock_bot)

    mock_cb.answer.assert_called_once()
    mock_cb.message.edit_text.assert_called_once()
    args, _ = mock_cb.message.edit_text.call_args
    assert "عضویتت تایید شد!" in args[0]


@pytest.mark.asyncio
async def test_admin_dashboard_callback(db_session):
    """Admin dashboard queries database and presents statistics."""
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.edit_text = AsyncMock()

    await handle_admin_dashboard(mock_cb)

    mock_cb.answer.assert_called_once()
    mock_cb.message.edit_text.assert_called_once()
    args, _ = mock_cb.message.edit_text.call_args
    assert "داشبورد وضعیت گوجه" in args[0]
