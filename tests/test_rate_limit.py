import pytest
from aiogram.types import Chat, Message, User
from goje.middleware.rate_limit import RateLimitMiddleware


def make_message(text: str, user_id: int = 100, chat_id: int = 200) -> Message:
    return Message(
        message_id=1,
        date=0,
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


@pytest.mark.asyncio
async def test_command_rate_limit_drops_after_limit(monkeypatch):
    monkeypatch.setattr(
        "goje.middleware.rate_limit.settings.RATE_LIMIT_COMMAND_COUNT", 2
    )
    monkeypatch.setattr(
        "goje.middleware.rate_limit.settings.RATE_LIMIT_COMMAND_WINDOW", 10
    )

    middleware = RateLimitMiddleware()
    calls = 0

    async def handler(event, data):
        nonlocal calls
        calls += 1
        return True

    event = make_message("/start")

    assert await middleware(handler, event, {}) is True
    assert await middleware(handler, event, {}) is True
    assert await middleware(handler, event, {}) is None
    assert calls == 2


@pytest.mark.asyncio
async def test_wake_word_has_separate_bucket(monkeypatch):
    monkeypatch.setattr(
        "goje.middleware.rate_limit.settings.RATE_LIMIT_COMMAND_COUNT", 1
    )
    monkeypatch.setattr(
        "goje.middleware.rate_limit.settings.RATE_LIMIT_WAKE_WORD_COUNT", 1
    )

    middleware = RateLimitMiddleware()
    calls = 0

    async def handler(event, data):
        nonlocal calls
        calls += 1
        return True

    assert await middleware(handler, make_message("/start"), {}) is True
    assert await middleware(handler, make_message("گوجه"), {}) is True
    assert calls == 2


def test_event_categories():
    middleware = RateLimitMiddleware()

    assert middleware._get_event_info(make_message("/admin"))[2] == "command"
    assert middleware._get_event_info(make_message("گوجه"))[2] == "wake_word"
    assert middleware._get_event_info(make_message("سلام"))[2] == "message"
