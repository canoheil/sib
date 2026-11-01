import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from sib.middleware.rate_limit import ThrottlingMiddleware


def make_message(text="سیب", user_id=100, chat_id=200):
    return Message(message_id=1, date=0, chat=Chat(id=chat_id, type="private"),
                   from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/cards", "/trade", "سیب", "سیب 2 100", "hello", None])
async def test_ten_rapid_messages(text):
    middleware = ThrottlingMiddleware()
    handler = AsyncMock()
    await asyncio.gather(*(middleware(handler, make_message(text), {}) for _ in range(10)))
    assert handler.await_count == 3


@pytest.mark.asyncio
async def test_shared_across_types_and_chats():
    middleware = ThrottlingMiddleware()
    handler = AsyncMock()
    for text, chat in [("/cards", 1), ("/trade", 2), ("سیب", 3)]:
        await middleware(handler, make_message(text, chat_id=chat), {})
    callback = CallbackQuery(id="1", from_user=make_message().from_user,
                             chat_instance="1", data="anything")
    await middleware(handler, callback, {})
    assert handler.await_count == 3
    await middleware(handler, make_message(user_id=101), {})
    assert handler.await_count == 4


@pytest.mark.asyncio
async def test_jail_deadline_silent_drops_and_expiry():
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    for _ in range(6):
        await middleware(handler, make_message(), {})
    state = middleware._users[100]
    assert state.strike_count == 3
    assert state.banned_until == 7300.0
    now[0] = 7299.0
    await middleware(handler, make_message(), {})
    assert handler.await_count == 3
    assert state.banned_until == 7300.0  # spam cannot extend the ban
    now[0] = 7300.0
    await middleware(handler, make_message(), {})
    assert handler.await_count == 4


@pytest.mark.asyncio
async def test_window_and_old_strikes_expire():
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    for _ in range(5):
        await middleware(handler, make_message(), {})
    now[0] += 2
    await middleware(handler, make_message(), {})
    assert handler.await_count == 4
    now[0] += 60
    for _ in range(4):
        await middleware(handler, make_message(), {})
    assert middleware._users[100].strike_count == 1
    assert middleware._users[100].banned_until == 0
