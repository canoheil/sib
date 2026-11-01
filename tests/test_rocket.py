"""Persian/Arabic digit parsing for the Rocket command."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sib.handlers import rocket as rocket_handler
from sib.services import rocket_service


@pytest.mark.parametrize(
    ("text", "expected_target", "expected_bet"),
    [
        ("سیب ۲ ۵۰", Decimal("2"), 50),
        ("سیب ۲.۵ ۱۰۰", Decimal("2.5"), 100),
        ("سیب ٢ ٥٠", Decimal("2"), 50),
        ("سیب ٢.٥ ١٠٠", Decimal("2.5"), 100),
    ],
)
def test_rocket_command_parses_persian_and_arabic_digits(
    text, expected_target, expected_bet
):
    assert rocket_handler.parse_rocket_command(text) == (
        expected_target,
        expected_bet,
    )
    assert rocket_handler._ROCKET_MESSAGE_FILTER.resolve(
        SimpleNamespace(text=text)
    ) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_target", "expected_bet"),
    [
        ("سیب ۲ ۵۰", Decimal("2"), 50),
        ("سیب ۲.۵ ۱۰۰", Decimal("2.5"), 100),
        ("سیب ٢ ٥٠", Decimal("2"), 50),
    ],
)
async def test_localized_digit_command_starts_rocket_game(
    monkeypatch, text, expected_target, expected_bet
):
    session = object()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    play_calls = []

    async def fake_play_rocket(
        actual_session, user_id, *, target_multiplier, bet_amount
    ):
        play_calls.append(
            (actual_session, user_id, target_multiplier, bet_amount)
        )
        return {
            "status": "started",
            "game_id": "localized-digit-round",
            "bet": bet_amount,
            "target": target_multiplier,
            "crash_point": 3.0,
        }

    monkeypatch.setattr(rocket_handler, "get_session", fake_get_session)
    monkeypatch.setattr(rocket_service, "play_rocket", fake_play_rocket)

    animation_calls = []

    async def fake_run_rocket_animation(bot, **kwargs):
        animation_calls.append((bot, kwargs))

    monkeypatch.setattr(
        rocket_handler, "run_rocket_animation", fake_run_rocket_animation
    )

    class MessageStub:
        def __init__(self):
            self.chat = SimpleNamespace(type="group")
            self.reply_to_message = None
            self.from_user = SimpleNamespace(id=7, is_bot=False)
            self.text = text
            self.animation_calls = []

        async def reply_animation(self, **kwargs):
            self.animation_calls.append(kwargs)
            return SimpleNamespace(
                chat=SimpleNamespace(id=-100), message_id=99
            )

        async def reply(self, *args, **kwargs):
            raise AssertionError("a valid game command should not be refused")

    message = MessageStub()
    bot = object()
    await rocket_handler.handle_rocket(message, bot)

    assert play_calls == [(session, 7, expected_target, expected_bet)]
    assert len(message.animation_calls) == 1
    assert message.animation_calls[0]["animation"].path == str(
        rocket_handler.LAUNCH_VIDEO
    )
    assert len(animation_calls) == 1
    assert animation_calls[0][0] is bot
    assert animation_calls[0][1]["game"]["target"] == expected_target
    assert animation_calls[0][1]["game"]["bet"] == expected_bet


@pytest.mark.asyncio
async def test_concurrent_rounds_are_dropped_and_guard_is_released(monkeypatch):
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def round_stub(message, bot, user_id):
        calls.append(user_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(rocket_handler, "_handle_rocket_round", round_stub)
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group"), reply_to_message=None,
        from_user=SimpleNamespace(id=9876, is_bot=False),
    )
    task = asyncio.create_task(rocket_handler.handle_rocket(message, object()))
    try:
        await entered.wait()
        await rocket_handler.handle_rocket(message, object())
        assert calls == [9876]
    finally:
        release.set()
        await task
    assert 9876 not in rocket_handler._ACTIVE_USERS
    await rocket_handler.handle_rocket(message, object())
    assert calls == [9876, 9876]


@pytest.mark.asyncio
async def test_cooldown_is_silent(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(rocket_handler, "get_session", session)
    monkeypatch.setattr(rocket_service, "play_rocket", AsyncMock(
        return_value={"status": "cooldown", "remaining_seconds": 30}))
    message = SimpleNamespace(
        text="سیب 2 100", chat=SimpleNamespace(type="group"),
        reply_to_message=None, from_user=SimpleNamespace(id=9877, is_bot=False),
        reply=AsyncMock(), reply_animation=AsyncMock(),
    )
    await rocket_handler.handle_rocket(message, object())
    message.reply.assert_not_awaited()
    message.reply_animation.assert_not_awaited()
