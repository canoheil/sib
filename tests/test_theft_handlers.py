"""Handler-level tests for the theft UI, the arrest window and its artwork.

The router's handler functions are called directly with light mocks (the same
pattern as ``tests/transfers/test_handlers.py``); no Telegram network and no
aiogram dispatcher is involved. Every database assertion reads through the
handler's own short-lived session, exactly like production.

The 10-second window is never really awaited: ``_arrest_window`` is replaced by
a controllable fake that releases the handler only after the arrest message has
been processed, so the "arrest during the window" flow is deterministic.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    SystemConfig,
    User,
    UserWantedState,
)
from sib.handlers import theft as handler
from sib.services import theft_service as theft

GROUP_ID = -770_001
OTHER_GROUP = -770_002
THIEF_ID = 11
VICTIM_ID = 22
ARRESTER_ID = 33

ROOT_MESSAGE_ID = 400
PENDING_MESSAGE_ID = 410


# ---------------------------------------------------------------------------
# Isolated database + patchable get_session
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(monkeypatch):
    """One in-memory database per test, wired into the handler's get_session."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = sa.MetaData()
    for model in (User, SystemConfig, AppleWallet, AppleTransaction, UserWantedState):
        model.__table__.to_metadata(metadata)
    # SQLite only autoincrements INTEGER primary keys; the ledger's BIGINT id
    # is narrowed in the cloned metadata, never in the application models.
    for table in metadata.tables.values():
        for column in table.columns:
            if column.primary_key and column.autoincrement is True:
                column.type = sa.Integer()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(handler, "get_session", session_context)
    yield factory
    await engine.dispose()


async def _seed(db, *, thief_level=0, thief_apples=0, victim_apples=4000,
                jailed_until=None, wanted_expires_at=None, **config):
    async with db.begin() as session:
        session.add_all(
            User(id=uid, first_name=name)
            for uid, name in (
                (THIEF_ID, "دزد"), (VICTIM_ID, "قربانی"), (ARRESTER_ID, "پلیس")
            )
        )
        await session.flush()
        session.add_all(
            AppleWallet(user_id=uid, balance=apples)
            for uid, apples in (
                (THIEF_ID, thief_apples), (VICTIM_ID, victim_apples), (ARRESTER_ID, 0)
            )
        )
        if thief_level or jailed_until or wanted_expires_at:
            session.add(
                UserWantedState(
                    user_id=THIEF_ID,
                    wanted_level=thief_level,
                    jailed_until=jailed_until,
                    wanted_expires_at=wanted_expires_at,
                    last_stolen_at=None,
                )
            )
        for key, value in config.items():
            session.add(SystemConfig(key=key, value=str(value)))


async def _balance(db, user_id):
    async with db.begin() as session:
        wallet = await session.get(AppleWallet, user_id)
        return wallet.balance if wallet else 0


async def _state(db, user_id):
    async with db.begin() as session:
        return await session.get(UserWantedState, user_id)


# ---------------------------------------------------------------------------
# Message fakes
# ---------------------------------------------------------------------------

def _message(uid=THIEF_ID, text="دزدی", *, target=None, chat_id=GROUP_ID,
             kind="supergroup", message_id=ROOT_MESSAGE_ID, sent_id=PENDING_MESSAGE_ID):
    message = MagicMock(spec=Message)
    message.chat = Chat(id=chat_id, type=kind)
    message.from_user = TelegramUser(id=uid, first_name=f"بازیکن {uid}", is_bot=False)
    message.text = text
    message.message_id = message_id
    sent = MagicMock(spec=Message)
    sent.message_id = sent_id
    sent.chat = message.chat
    message.reply = AsyncMock(return_value=sent)
    message.answer = AsyncMock(return_value=sent)
    message.reply_photo = AsyncMock(return_value=sent)
    message.answer_photo = AsyncMock(return_value=sent)
    if target is None:
        message.reply_to_message = None
    else:
        reply = MagicMock(spec=Message)
        reply.from_user = TelegramUser(id=target, first_name=f"بازیکن {target}", is_bot=False)
        reply.message_id = message_id - 1
        message.reply_to_message = reply
    return message


def _arrest(arrester_id=ARRESTER_ID, *, target_message_id=PENDING_MESSAGE_ID,
            chat_id=GROUP_ID):
    message = _message(arrester_id, "دستگیری", chat_id=chat_id)
    reply = MagicMock(spec=Message)
    reply.message_id = target_message_id
    message.reply_to_message = reply
    return message


class WindowFake:
    """Replacement for ``handler._arrest_window`` (never sleeps for real)."""

    def __init__(self):
        self.release = asyncio.Event()
        self.calls = []

    async def __call__(self, active, seconds):
        self.calls.append((active.amount, seconds))
        await self.release.wait()


@pytest.fixture(autouse=True)
def clear_active_thefts():
    handler.ACTIVE_THEFTS.clear()
    yield
    handler.ACTIVE_THEFTS.clear()


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["theft_success.jpg", "theft_fail.jpg", "jail.jpg"]
)
def test_artwork_is_1024_rgb_jpeg(name):
    path = handler.ASSET_DIR / name
    assert path.is_file()
    with Image.open(path) as image:
        assert image.size == (1024, 1024)
        assert image.mode == "RGB"
        assert image.format == "JPEG"


def test_artwork_paths_are_the_generated_files():
    assert handler.SUCCESS_IMAGE.name == "theft_success.jpg"
    assert handler.FAIL_IMAGE.name == "theft_fail.jpg"
    assert handler.JAIL_IMAGE.name == "jail.jpg"


# ---------------------------------------------------------------------------
# /wanted and «سیب جرم»
# ---------------------------------------------------------------------------

async def test_wanted_for_a_clean_player(db):
    await _seed(db)
    message = _message(text="/wanted")

    await handler.handle_wanted(message)

    message.reply.assert_awaited_once_with(handler.CLEAN_STATUS_TEXT)
    message.reply_photo.assert_not_awaited()


async def test_wanted_alias_سیب_جرم_matches_the_same_handler():
    assert handler.is_valid_trigger(handler.WANTED_PATTERN, "سیب جرم")
    assert handler.is_valid_trigger(handler.WANTED_PATTERN, "  سیب جرم ")
    assert not handler.is_valid_trigger(handler.WANTED_PATTERN, "سیب جرم من")


@pytest.mark.parametrize("level,rate", [(1, 90), (2, 80), (3, 70), (4, 60), (5, 50)])
async def test_wanted_for_a_wanted_player_shows_rate_and_expiry(db, monkeypatch, level, rate):
    moment = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    await _seed(
        db, thief_level=level,
        wanted_expires_at=(moment + timedelta(hours=3, minutes=30)).replace(tzinfo=None),
    )
    monkeypatch.setattr(theft, "_now", lambda: moment)
    message = _message(text="/wanted")

    await handler.handle_wanted(message)

    text = message.reply.await_args.args[0]
    # Visual stars: one ⭐ per star of the wanted level, no digit.
    assert f"وضعیت تعقیب: {'⭐' * level}" in text
    assert f"وضعیت تعقیب: {handler.fa_digits(level)} ⭐" not in text
    assert f"شانس موفقیت در دزدی بعدی: {handler.fa_digits(rate)}%" in text
    assert "زمان سوختن ستاره‌ها: ۳ ساعت و ۳۰ دقیقه باقی‌مانده" in text
    assert "زندان" not in text


async def test_wanted_for_a_jailed_player_adds_the_jail_line(db, monkeypatch):
    moment = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    await _seed(
        db, thief_level=2,
        wanted_expires_at=(moment + timedelta(hours=5)).replace(tzinfo=None),
        jailed_until=(moment + timedelta(hours=2)).replace(tzinfo=None),
    )
    monkeypatch.setattr(theft, "_now", lambda: moment)
    message = _message(text="سیب جرم")

    await handler.handle_wanted(message)

    text = message.reply.await_args.args[0]
    assert "⛓️ شما در زندان هستید! زمان آزادی: ۲ ساعت" in text
    assert "زمان سوختن ستاره‌ها: ۵ ساعت باقی‌مانده" in text


async def test_wanted_survives_a_database_failure():
    message = _message(text="/wanted")
    monkey_patch = pytest.MonkeyPatch()

    @asynccontextmanager
    async def boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkey_patch.setattr(handler, "get_session", boom)
    try:
        await handler.handle_wanted(message)
    finally:
        monkey_patch.undo()
    message.reply.assert_awaited_once_with(handler.GENERIC_ERROR)


def test_render_status_is_pure_and_formats_durations():
    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    clean = {"wanted_level": 0, "is_jailed": False, "jail_remaining_seconds": 0}
    assert handler.render_status(clean, wanted_expires_at=None) == handler.CLEAN_STATUS_TEXT

    wanted = {"wanted_level": 5, "is_jailed": False, "jail_remaining_seconds": 0}
    text = handler.render_status(
        wanted, wanted_expires_at=now + timedelta(minutes=12), now=now
    )
    assert "شانس موفقیت در دزدی بعدی: ۵۰%" in text
    assert "۱۲ دقیقه باقی‌مانده" in text

    expired = handler.render_status(wanted, wanted_expires_at=now - timedelta(minutes=1), now=now)
    assert "۰ ثانیه باقی‌مانده" in expired

    # One ⭐ per star, capped at five, and never a digit.
    for level in (1, 2, 3, 4, 5):
        line = handler.render_status(
            {"wanted_level": level, "is_jailed": False, "jail_remaining_seconds": 0},
            wanted_expires_at=now + timedelta(hours=1), now=now,
        ).splitlines()[0]
        assert line == f"وضعیت تعقیب: {'⭐' * level}"
    assert handler.render_status(
        {"wanted_level": 9, "is_jailed": False, "jail_remaining_seconds": 0},
        wanted_expires_at=now + timedelta(hours=1), now=now,
    ).splitlines()[0] == "وضعیت تعقیب: ⭐⭐⭐⭐⭐"

    # A clean player keeps the plain text.
    assert handler.render_status(clean, wanted_expires_at=None) == handler.CLEAN_STATUS_TEXT


# ---------------------------------------------------------------------------
# Theft: guards
# ---------------------------------------------------------------------------

async def test_theft_requires_a_group_and_a_reply(db):
    await _seed(db)
    private = _message(target=VICTIM_ID, kind="private")
    await handler.handle_theft(private)
    private.reply.assert_awaited_once_with(handler.PRIVATE_CHAT_TEXT)

    no_reply = _message()
    await handler.handle_theft(no_reply)
    no_reply.reply.assert_awaited_once_with(handler.REPLY_REQUIRED_TEXT)


async def test_thief_cannot_target_themselves(db):
    await _seed(db)
    message = _message(target=THIEF_ID)

    await handler.handle_theft(message)

    message.reply.assert_awaited_once_with(handler.SELF_TEXT)
    assert await _balance(db, VICTIM_ID) == 4000
    assert await _balance(db, THIEF_ID) == 0


async def test_jailed_thief_is_refused(db):
    await _seed(db, jailed_until=datetime.utcnow() + timedelta(hours=1))
    message = _message(target=VICTIM_ID)

    await handler.handle_theft(message)

    message.reply.assert_awaited_once_with(handler.JAILED_TEXT)
    message.reply_photo.assert_not_awaited()
    assert await _balance(db, VICTIM_ID) == 4000


async def test_cooldown_is_reported(db):
    await _seed(db, thief_level=1)
    async with db.begin() as session:
        state = await session.get(UserWantedState, THIEF_ID)
        state.last_stolen_at = datetime.utcnow() - timedelta(minutes=2)
        state.wanted_expires_at = datetime.utcnow() + timedelta(hours=20)
    message = _message(target=VICTIM_ID)

    await handler.handle_theft(message)

    message.reply.assert_awaited_once_with(handler.COOLDOWN_TEXT)
    assert await _balance(db, VICTIM_ID) == 4000


async def test_insufficient_victim_wallet_is_reported(db):
    await _seed(db, victim_apples=10)
    message = _message(target=VICTIM_ID)

    await handler.handle_theft(message)

    message.reply.assert_awaited_once_with(handler.INSUFFICIENT_TEXT)
    assert await _balance(db, VICTIM_ID) == 10


# ---------------------------------------------------------------------------
# Theft: instant success (level 0-1)
# ---------------------------------------------------------------------------

async def test_clean_thief_theft_succeeds_instantly_with_artwork(db):
    # Zero stars: the only instant theft left after the fix (bug 1).
    await _seed(db)
    message = _message(target=VICTIM_ID)

    await handler.handle_theft(message)

    assert await _balance(db, VICTIM_ID) == 3800
    assert await _balance(db, THIEF_ID) == 200
    assert (await _state(db, THIEF_ID)).wanted_level == 1

    message.reply_photo.assert_awaited_once()
    args, kwargs = message.reply_photo.await_args
    assert str(args[0].path) == str(handler.SUCCESS_IMAGE)
    assert kwargs["parse_mode"] == "HTML"
    caption = kwargs["caption"]
    assert "دزدی موفق!" in caption
    assert "۲۰۰ سیب از قربانی" in caption
    assert "۱ ستاره ⭐" in caption
    message.answer_photo.assert_not_awaited()


# ---------------------------------------------------------------------------
# Theft: pending window (level 2+)
# ---------------------------------------------------------------------------

async def test_second_theft_opens_the_window_instead_of_paying_out(db, monkeypatch):
    """Regression (bug 1): a one-star thief's theft must wait for the window."""
    await _seed(db, thief_level=1, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    message.reply_photo.assert_awaited_once()
    caption = message.reply_photo.await_args.kwargs["caption"]
    assert "دزدی در جریان است" in caption
    assert window.calls == [(200, 10)]
    assert await _balance(db, VICTIM_ID) == 4000  # no instant payout
    assert await _balance(db, THIEF_ID) == 0
    assert (await _state(db, THIEF_ID)).wanted_level == 1

    window.release.set()
    await task


async def test_pending_theft_moves_no_money_and_opens_the_window(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    # Mid-window: artwork posted, no apple moved, nothing resolved yet.
    message.reply_photo.assert_awaited_once()
    caption = message.reply_photo.await_args.kwargs["caption"]
    assert "دزدی در جریان است" in caption
    assert "«دستگیری»" in caption
    assert window.calls == [(200, 10)]
    assert await _balance(db, VICTIM_ID) == 4000
    assert await _balance(db, THIEF_ID) == 0
    assert (await _state(db, THIEF_ID)).wanted_level == 2
    assert set(handler.ACTIVE_THEFTS) == {(GROUP_ID, PENDING_MESSAGE_ID)}

    window.release.set()
    await task
    assert handler.ACTIVE_THEFTS == {}


async def test_window_uses_the_configured_seconds(db, monkeypatch):
    await _seed(
        db, thief_level=2,
        wanted_expires_at=datetime.utcnow() + timedelta(hours=6),
        theft_arrest_window_seconds="7",
    )
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)
    assert window.calls == [(200, 7)]
    window.release.set()
    await task


async def test_arrest_during_the_window_jails_the_thief(db, monkeypatch):
    await _seed(db, thief_level=3, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    arrest = _arrest()
    await handler.handle_arrest(arrest)
    arrest.reply.assert_awaited_once_with(handler.ARREST_REGISTERED_TEXT)
    assert handler.ACTIVE_THEFTS[(GROUP_ID, PENDING_MESSAGE_ID)].caught_by_id == ARRESTER_ID

    window.release.set()
    await task

    # The victim keeps everything, the bot pays the 3-star bounty, thief jailed.
    assert await _balance(db, VICTIM_ID) == 4000
    assert await _balance(db, THIEF_ID) == 0
    assert await _balance(db, ARRESTER_ID) == 300
    state = await _state(db, THIEF_ID)
    assert state.wanted_level == 0
    assert state.jailed_until is not None

    message.answer_photo.assert_awaited_once()
    args, kwargs = message.answer_photo.await_args
    assert str(args[0].path) == str(handler.JAIL_IMAGE)
    caption = kwargs["caption"]
    assert "را دستگیر کرد" in caption
    assert "پلیس" in caption and "دزد" in caption
    assert "۳۰۰ سیب" in caption
    assert "۲ ساعت زندانی" in caption


async def test_the_victim_cannot_arrest(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    arrest = _arrest(arrester_id=VICTIM_ID)
    await handler.handle_arrest(arrest)
    arrest.reply.assert_awaited_once_with(handler.VICTIM_CANNOT_ARREST_TEXT)
    theft_state = handler.ACTIVE_THEFTS[(GROUP_ID, PENDING_MESSAGE_ID)]
    assert theft_state.caught_by_id is None

    window.release.set()
    await task
    assert await _balance(db, VICTIM_ID) in (4000, 3800)  # the RNG decides


async def test_the_thief_cannot_arrest_themselves(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    arrest = _arrest(arrester_id=THIEF_ID)
    await handler.handle_arrest(arrest)
    arrest.reply.assert_not_awaited()
    assert handler.ACTIVE_THEFTS[(GROUP_ID, PENDING_MESSAGE_ID)].caught_by_id is None

    window.release.set()
    await task


async def test_second_arrest_is_refused(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    first = _arrest()
    await handler.handle_arrest(first)
    second = _arrest(arrester_id=99)
    await handler.handle_arrest(second)
    second.reply.assert_awaited_once_with(handler.ALREADY_ARRESTED_TEXT)
    assert handler.ACTIVE_THEFTS[(GROUP_ID, PENDING_MESSAGE_ID)].caught_by_id == ARRESTER_ID

    window.release.set()
    await task


async def test_arrest_on_an_unrelated_message_is_silent(db):
    await _seed(db)
    arrest = _arrest(target_message_id=ROOT_MESSAGE_ID)
    await handler.handle_arrest(arrest)
    arrest.reply.assert_not_awaited()


async def test_arrest_in_another_group_is_silent(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    arrest = _arrest(chat_id=OTHER_GROUP)
    await handler.handle_arrest(arrest)
    arrest.reply.assert_not_awaited()

    window.release.set()
    await task


async def test_nobody_arrests_and_the_rng_success_pays_out(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    monkeypatch.setattr(theft.random, "random", lambda: 0.0)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)
    window.release.set()
    await task

    assert await _balance(db, VICTIM_ID) == 3800
    assert await _balance(db, THIEF_ID) == 200
    assert (await _state(db, THIEF_ID)).wanted_level == 3

    message.answer_photo.assert_awaited_once()
    args, kwargs = message.answer_photo.await_args
    assert str(args[0].path) == str(handler.SUCCESS_IMAGE)
    assert "دزدی موفق! ۲۰۰ سیب دزدیده شد." in kwargs["caption"]


async def test_nobody_arrests_and_the_rng_fails(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    monkeypatch.setattr(theft.random, "random", lambda: 0.99)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)
    window.release.set()
    await task

    assert await _balance(db, VICTIM_ID) == 4000
    assert await _balance(db, THIEF_ID) == 0
    assert (await _state(db, THIEF_ID)).wanted_level == 2

    message.answer_photo.assert_awaited_once()
    args, kwargs = message.answer_photo.await_args
    assert str(args[0].path) == str(handler.FAIL_IMAGE)
    assert kwargs["caption"] == handler.WINDOW_FAIL_CAPTION


async def test_settlement_failure_is_reported_and_clears_the_window(db, monkeypatch):
    await _seed(db, thief_level=2, wanted_expires_at=datetime.utcnow() + timedelta(hours=6))
    window = WindowFake()
    monkeypatch.setattr(handler, "_arrest_window", window)
    message = _message(target=VICTIM_ID)

    task = asyncio.create_task(handler.handle_theft(message))
    await asyncio.sleep(0.05)

    async def boom(session, thief_id, victim_id, amount, caught_by_id):
        raise RuntimeError("settlement exploded")

    monkeypatch.setattr(handler.theft_service, "resolve_pending_theft", boom)
    window.release.set()
    await task

    assert handler.ACTIVE_THEFTS == {}
    message.reply.assert_awaited_with(handler.GENERIC_ERROR)
    assert await _balance(db, VICTIM_ID) == 4000


# ---------------------------------------------------------------------------
# The real window helper (event-driven, never a blocking sleep)
# ---------------------------------------------------------------------------

def _active_theft():
    return handler.ActiveTheft(
        chat_id=GROUP_ID, message_id=PENDING_MESSAGE_ID,
        thief_id=THIEF_ID, victim_id=VICTIM_ID, amount=200,
    )


async def test_arrest_window_ends_early_on_a_successful_arrest():
    active = _active_theft()
    task = asyncio.create_task(handler._arrest_window(active, 30))
    await asyncio.sleep(0.05)
    active.arrested.set()
    # Ends long before the 30 seconds it was given.
    await asyncio.wait_for(task, timeout=2)
    assert task.done() and not task.cancelled()


async def test_arrest_window_returns_immediately_for_zero_seconds():
    await asyncio.wait_for(handler._arrest_window(_active_theft(), 0), timeout=1)


async def test_arrest_window_completes_when_nobody_arrives():
    await asyncio.wait_for(handler._arrest_window(_active_theft(), 0.05), timeout=2)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_router_is_registered_and_triggers_are_anchored():
    from aiogram import Dispatcher

    from sib.handlers import register_all_handlers

    dp = Dispatcher()
    register_all_handlers(dp)
    assert handler.router in dp.sub_routers
    assert handler.router.name == "theft"

    for text in ("دزدی", "دزدی سیبی", " دزدی  "):
        assert handler.is_valid_trigger(handler.THEFT_PATTERN, text)
    for text in ("دزدی سیب", "من دزدی کردم", "دستگیری"):
        assert not handler.is_valid_trigger(handler.THEFT_PATTERN, text)
    assert handler.is_valid_trigger(handler.ARREST_PATTERN, "دستگیری")
    assert not handler.is_valid_trigger(handler.ARREST_PATTERN, "دستگیری کن")
