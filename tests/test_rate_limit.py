import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from sib.middleware.rate_limit import (
    WARNING_TEXT,
    ThrottlingMiddleware,
)


def make_message(text="سیب", user_id=100, chat_id=200):
    return Message(message_id=1, date=0, chat=Chat(id=chat_id, type="private"),
                   from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)


def make_callback(user_id=100, data="casino:spin"):
    return CallbackQuery(id="1", from_user=make_message(user_id=user_id).from_user,
                         chat_instance="1", data=data)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/cards", "/trade", "سیب", "سیب 2 100", "hello", None])
async def test_ten_rapid_messages(text):
    """LIMIT events per WINDOW pass; the rest are dropped as strikes."""
    middleware = ThrottlingMiddleware()
    handler = AsyncMock()
    await asyncio.gather(*(middleware(handler, make_message(text), {}) for _ in range(10)))
    assert handler.await_count == 6  # LIMIT = 6 per 2 s (was 3)


@pytest.mark.asyncio
async def test_shared_across_types_and_chats():
    middleware = ThrottlingMiddleware()
    handler = AsyncMock()
    for text, chat in [("/cards", 1), ("/trade", 2), ("سیب", 3)]:
        await middleware(handler, make_message(text, chat_id=chat), {})
    await middleware(handler, make_callback(), {})
    assert handler.await_count == 4  # 4 events <= LIMIT 6, mixed types share the budget
    await middleware(handler, make_message(user_id=101), {})
    assert handler.await_count == 5


@pytest.mark.asyncio
async def test_jail_deadline_drops_and_expiry():
    """5 strikes within 60 s jail for 10 minutes; the ban cannot be extended
    by more spam and lifts at the deadline."""
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    for _ in range(11):  # 6 pass, then 5 strikes -> jail
        await middleware(handler, make_message(), {})
    state = middleware._users[100]
    assert handler.await_count == 6
    assert state.strike_count == 5
    assert state.banned_until == 100.0 + 600.0  # BAN_SECONDS = 10 min
    now[0] = 699.0
    await middleware(handler, make_message(), {})
    assert handler.await_count == 6
    assert state.banned_until == 700.0  # spam cannot extend the ban
    now[0] = 700.0
    await middleware(handler, make_message(), {})
    assert handler.await_count == 7


@pytest.mark.asyncio
async def test_window_and_old_strikes_expire():
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    for _ in range(8):  # 6 pass, 2 strikes
        await middleware(handler, make_message(), {})
    assert handler.await_count == 6
    assert middleware._users[100].strike_count == 2
    now[0] += 2
    await middleware(handler, make_message(), {})
    assert handler.await_count == 7
    now[0] += 60
    for _ in range(6):
        await middleware(handler, make_message(), {})
    assert middleware._users[100].strike_count == 0  # old strikes expired
    assert middleware._users[100].banned_until == 0


@pytest.mark.asyncio
async def test_strike_warning_is_sent_once_per_minute(monkeypatch):
    """Hitting the rate limit warns «⚠️ لطفاً کمی آهسته‌تر!» — at most once
    per minute, even under a burst of strikes."""
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)

    for _ in range(8):  # 6 pass + 2 strikes
        await middleware(handler, make_message(), {})
    assert handler.await_count == 6
    answer.assert_awaited_once_with(WARNING_TEXT)

    now[0] += 61.0
    for _ in range(7):  # refill to LIMIT, then one more strike
        await middleware(handler, make_message(), {})
    assert handler.await_count == 12
    assert answer.await_count == 2  # a minute later it warns again
    assert answer.await_args.args[0] == WARNING_TEXT


@pytest.mark.asyncio
async def test_jail_notice_is_sent_when_jailed(monkeypatch):
    """Entering the jail sends the 10-minute notice (Persian digits)."""
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)

    for _ in range(11):  # 6 pass + 5 strikes -> jail
        await middleware(handler, make_message(), {})
    assert handler.await_count == 6
    assert middleware._users[100].banned_until == 700.0
    # First strike warned, the fifth (jail) notice is always sent.
    assert answer.await_count == 2
    answer.assert_any_await(WARNING_TEXT)
    jail_call = answer.await_args_list[-1]
    assert jail_call.args[0] == middleware.jail_text()
    assert "۱۰" in jail_call.args[0]  # «10 minutes» in Persian digits
    assert "محروم" in jail_call.args[0]


@pytest.mark.asyncio
async def test_callback_is_alerted_never_sent_a_chat_message(monkeypatch):
    """For a CallbackQuery the UX feedback is a callback ALERT, not a chat
    message — the inline UI is not disturbed."""
    now = [100.0]
    middleware = ThrottlingMiddleware(clock=lambda: now[0])
    handler = AsyncMock()
    cb_answer = AsyncMock()
    msg_answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", cb_answer)
    monkeypatch.setattr(Message, "answer", msg_answer)

    for _ in range(11):  # 6 pass + 5 strikes -> jail
        await middleware(handler, make_callback(), {})
    assert handler.await_count == 6
    assert cb_answer.await_count == 2  # warning alert + jail alert
    for call in cb_answer.await_args_list:
        assert call.kwargs.get("show_alert") is True
    assert cb_answer.await_args_list[0].args[0] == WARNING_TEXT
    assert cb_answer.await_args_list[-1].args[0] == middleware.jail_text()
    msg_answer.assert_not_awaited()  # no chat message was sent for a callback
