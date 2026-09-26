"""Tests for owner-scoped interactive panels (Phase 3).

Spec T: a panel opened by a specific user must only be interactable and
closable by that user, while public flows (2v2/4v4 joins, matchmaking
acceptance, game moves) must remain accessible to everyone entitled.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser

from sib.database.connection import get_session
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services.panel import (
    REJECT_MESSAGE,
    PanelOwnershipService,
    is_owner_scoped_callback,
)


def _callback(user_id: int, data: str, chat_id: int = -100500, message_id: int = 900):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="کاربر")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type="supergroup", title="گروه")
    cb.message.message_id = message_id
    cb.answer = AsyncMock()
    return cb


async def _run_guard(callback):
    """Run the panel guard middleware with a spy handler."""
    called = []

    async def handler(event, data):
        called.append(event)
        return "HANDLED"

    middleware = PanelGuardMiddleware()
    result = await middleware(handler, callback, {})
    return result, called


def test_callback_classification():
    # Owner-scoped controls
    assert is_owner_scoped_callback("close_panel")
    assert is_owner_scoped_callback("create_new_party_menu")
    assert is_owner_scoped_callback("select_game:battleship")
    assert is_owner_scoped_callback("create_party:battleship:2v2")
    # Public flows must stay open
    assert not is_owner_scoped_callback("join_party:12")
    assert not is_owner_scoped_callback("view_party:12")
    assert not is_owner_scoped_callback("group_games_panel")
    assert not is_owner_scoped_callback("accept_match:1:1")
    assert not is_owner_scoped_callback("reject_match:1")
    assert not is_owner_scoped_callback("find_match:12")
    assert not is_owner_scoped_callback("ttt_action:1:0")
    assert not is_owner_scoped_callback("game_refresh:1")
    assert not is_owner_scoped_callback(None)


@pytest.mark.asyncio
async def test_first_user_claims_panel(db_session):
    ok_a = await PanelOwnershipService.claim(db_session, -1, 42, user_id=1, panel_type="test")
    ok_b = await PanelOwnershipService.claim(db_session, -1, 42, user_id=2, panel_type="test")
    await db_session.commit()

    assert ok_a is True
    assert ok_b is False
    assert await PanelOwnershipService.get_owner(db_session, -1, 42) == 1


@pytest.mark.asyncio
async def test_non_owner_cannot_mutate_panel(db_session):
    cb_a = _callback(user_id=111, data="create_new_party_menu", chat_id=-200, message_id=55)
    result, called = await _run_guard(cb_a)
    assert result == "HANDLED" and called  # A claims and proceeds

    cb_b = _callback(user_id=222, data="select_game:battleship", chat_id=-200, message_id=55)
    result_b, called_b = await _run_guard(cb_b)
    assert result_b is None
    assert not called_b  # handler never ran for B
    assert cb_b.answer.called
    assert cb_b.answer.call_args[0][0] == REJECT_MESSAGE


@pytest.mark.asyncio
async def test_owner_can_use_their_panel(db_session):
    cb_a1 = _callback(user_id=111, data="create_new_party_menu", chat_id=-201, message_id=56)
    await _run_guard(cb_a1)

    cb_a2 = _callback(user_id=111, data="select_game:tic_tac_toe", chat_id=-201, message_id=56)
    result, called = await _run_guard(cb_a2)
    assert result == "HANDLED" and called


@pytest.mark.asyncio
async def test_non_owner_cannot_close_panel(db_session):
    cb_a = _callback(user_id=111, data="create_new_party_menu", chat_id=-202, message_id=57)
    await _run_guard(cb_a)

    cb_b = _callback(user_id=222, data="close_panel", chat_id=-202, message_id=57)
    result_b, called_b = await _run_guard(cb_b)
    assert result_b is None and not called_b

    cb_a_close = _callback(user_id=111, data="close_panel", chat_id=-202, message_id=57)
    result_a, called_a = await _run_guard(cb_a_close)
    assert result_a == "HANDLED" and called_a


@pytest.mark.asyncio
async def test_public_join_is_not_blocked(db_session):
    # A owns the panel message...
    cb_a = _callback(user_id=111, data="create_new_party_menu", chat_id=-203, message_id=58)
    await _run_guard(cb_a)

    # ...but B can still press public join/view buttons on it.
    for data in ("join_party:1", "view_party:1", "find_match:1", "accept_match:1:1"):
        cb_b = _callback(user_id=222, data=data, chat_id=-203, message_id=58)
        result, called = await _run_guard(cb_b)
        assert result == "HANDLED", data
        assert called, data


@pytest.mark.asyncio
async def test_released_panel_becomes_shared(db_session):
    cb_a = _callback(user_id=111, data="create_new_party_menu", chat_id=-204, message_id=59)
    await _run_guard(cb_a)

    async with get_session() as session:
        await PanelOwnershipService.release(session, -204, 59)

    # After the shared group panel is restored, anyone may use the menu.
    cb_b = _callback(user_id=222, data="create_new_party_menu", chat_id=-204, message_id=59)
    result, called = await _run_guard(cb_b)
    assert result == "HANDLED" and called


@pytest.mark.asyncio
async def test_callbacks_without_message_are_not_blocked(db_session):
    cb = _callback(user_id=111, data="close_panel", chat_id=-205, message_id=60)
    cb.message = None
    result, called = await _run_guard(cb)
    assert result == "HANDLED" and called
