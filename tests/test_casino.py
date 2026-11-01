from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from sib.database.models import AppleTransaction, AppleWallet, User
from sib.handlers import casino
from sib.services import casino_service
from sib.services.apple_wallet import AppleWalletService


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    col = AppleTransaction.__table__.c.id
    old = col.type
    col.type = sa.Integer()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: User.metadata.create_all(c, tables=[User.__table__, AppleWallet.__table__, AppleTransaction.__table__]))
    finally:
        col.type = old
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory.begin() as session:
        session.add(User(id=1, first_name="test"))
        await session.flush()
        await AppleWalletService.earn(session, 1, 10000, reason="test")
    @asynccontextmanager
    async def get_session():
        async with factory.begin() as session:
            yield session
    monkeypatch.setattr(casino, "get_session", get_session)
    yield factory
    await engine.dispose()


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("key,emoji", casino.GAMES.items())
async def test_callback(key, emoji, state):
    callback = SimpleNamespace(data=f"casino_game:{key}", message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    await casino.choose_game(callback, state)
    assert await state.get_state() == casino.CasinoStates.waiting_for_bet.state
    assert (await state.get_data())["emoji"] == emoji
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu(state):
    message = SimpleNamespace(answer_photo=AsyncMock())
    await casino.casino_menu(message, state)
    kwargs = message.answer_photo.await_args.kwargs
    assert len(kwargs["reply_markup"].inline_keyboard[0]) == 4
    assert casino.ASSET.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji,value,won", [(e, v, w) for e in casino.GAMES.values() for v, w in ([(22, True), (1, False)] if e == "🎰" else [(6, True), (1, False)])])
@pytest.mark.parametrize("bet_text", ["۱۰", "10"])
async def test_round(db, state, emoji, value, won, bet_text):
    await state.update_data(emoji=emoji)
    await state.set_state(casino.CasinoStates.waiting_for_bet)
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text=bet_text, answer=AsyncMock(), answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=value))))
    await casino.place_bet(message, state)
    payout = (30 if emoji == "🎰" else 18) if won else 0
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 10 + payout
    assert ("بردی" if won else "باختی") in message.answer.await_args.args[0]
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_refusals_and_cooldown(db):
    async with db.begin() as session:
        for amount in (0, 9, 5001, -1, 1.5, True):
            assert (await casino_service.validate_bet(session, 1, amount))["status"] == "invalid_bet"
        assert (await casino_service.validate_bet(session, 1, 5000))["status"] == "started"
    async with db.begin() as session:
        assert (await casino_service.validate_bet(session, 1, 10))["status"] == "cooldown"
        assert (await casino_service.validate_bet(session, 999, 10))["status"] == "not_found"
    async with db.begin() as session:
        session.add(User(id=2, first_name="poor"))
        await session.flush()
        assert (await casino_service.validate_bet(session, 2, 10))["status"] == "insufficient_funds"


@pytest.mark.parametrize("value", [22, 43, 64])
def test_slots_winning_values(value):
    assert casino_service.calculate_result("🎰", value, 11)["payout"] == 33


@pytest.mark.asyncio
async def test_send_failure_refunds(db, state):
    await state.update_data(emoji="🎲")
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="10", answer=AsyncMock(), answer_dice=AsyncMock(side_effect=RuntimeError("send failed")))
    await casino.place_bet(message, state)
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/casino", "قمارخانه", "قمارخونه", "قمار خانه", "قمار خونه", "قمار"])
async def test_menu_filters(text):
    from aiogram import Bot
    from aiogram.types import Chat, Message, User as TelegramUser
    from datetime import datetime
    message = Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"), from_user=TelegramUser(id=1, is_bot=False, first_name="test"), text=text)
    bot = Bot("123456:ABCdef_123456789012345678901234567")
    try:
        matches = [await h.check(message, bot=bot) for h in casino.router.message.handlers if h.callback is casino.casino_menu]
        assert any(match for match, _ in matches)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["abc", "1.5", "-10", "۵۰۰۱", "9"])
async def test_invalid_inputs_clear_state(db, state, text):
    await state.update_data(emoji="🎲")
    await state.set_state(casino.CasinoStates.waiting_for_bet)
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text=text, answer=AsyncMock(), answer_dice=AsyncMock())
    await casino.place_bet(message, state)
    message.answer_dice.assert_not_awaited()
    assert await state.get_state() is None
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000
