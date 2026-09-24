"""Tests for Party Telegram Handlers and UX interactions."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import Message, CallbackQuery, User as TelegramUser, Chat as TelegramChat
from sib.database.models import PartyStatus
from sib.handlers.party.handlers import (
    cmd_group_games,
    handle_create_party,
    handle_join_party,
    handle_leave_party,
    handle_cancel_party,
    handle_games_panel,
)
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_cmd_games_in_group(db_session):
    """Calling /games inside group returns game panel."""
    group = await create_test_group(db_session, "گروه تست پنل")
    user = await create_test_user(db_session, "امیر")

    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=group.id, type="supergroup", title=group.title)
    mock_msg.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    mock_msg.reply = AsyncMock()

    await cmd_group_games(mock_msg)

    mock_msg.reply.assert_called_once()
    args, kwargs = mock_msg.reply.call_args
    assert "منوی بازی‌های سیب" in args[0] or "بازی‌های فعال" in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_handle_create_party_callback(db_session):
    """Clicking create_party callback creates party and updates message to party card."""
    group = await create_test_group(db_session, "گروه ساخت پارتی")
    user = await create_test_user(db_session, "بهزاد")

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = "create_party:connect_four:2v2"
    mock_cb.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.chat = TelegramChat(id=group.id, type="supergroup", title=group.title)
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_create_party(mock_cb)

    mock_cb.answer.assert_called_once()
    mock_cb.message.edit_text.assert_called_once()
    args, kwargs = mock_cb.message.edit_text.call_args
    assert "پارتی بازی گروه" in args[0] or "تیم کامل شد" in args[0]
    assert "چهار در یک ردیف" in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_handle_join_party_callback(db_session):
    """Clicking join_party callback adds user to party and updates card."""
    group = await create_test_group(db_session, "گروه پیوستن")
    u1 = await create_test_user(db_session, "سازنده")
    u2 = await create_test_user(db_session, "ملحق‌شونده")

    # Create 2v2 party
    from sib.services.party import PartyService
    party = await PartyService.create_party(db_session, group.id, u1.id, "connect_four", "2v2")
    await db_session.commit()

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"join_party:{party.id}"
    mock_cb.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.chat = TelegramChat(id=group.id, type="supergroup", title=group.title)
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_join_party(mock_cb)

    mock_cb.answer.assert_called_once()
    mock_cb.message.edit_text.assert_called_once()
    args, kwargs = mock_cb.message.edit_text.call_args
    assert "تیم کامل شد!" in args[0]
    assert "چهار در یک ردیف" in args[0]
    assert u2.first_name in args[0]
