"""REF2 - friend referral system tests (reward paid on /start).

Isolated in-memory SQLite (same pattern as the castle/war suites): the
deep-link ``/start`` handler, the referral service, the invite-friends UI
and the 051 migration run for real; only Telegram I/O is mocked.

REF2 softened the REF1 anti-cheat rule: the inviter is paid as soon as
the invitee presses ``/start`` in the private chat - no group activity is
required any more - while the payment stays exactly-once.
"""

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
import pytest_asyncio
from aiogram.enums import ChatType
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import sib.handlers.start as start_module
from sib.core.config import settings
from sib.database.models import AppleTransaction, User
from sib.handlers.start import handle_start, handle_start_deep_link
from sib.keyboards.common import get_profile_keyboard
from sib.services.apple_wallet import AppleWalletService
from sib.services.membership.checker import MembershipService
from sib.services.referral import (
    INVITEE_BONUS_TEXT,
    INVITEE_REWARD_APPLES,
    INVITER_NOTIFICATION_TEXT,
    INVITER_REWARD_APPLES,
    ReferralService,
)

INVITER_ID = 1001
INVITEE_ID = 2002
SECOND_INVITEE_ID = 2003
GROUP_ID = -1001234567


@pytest_asyncio.fixture
async def ref_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    async with engine.begin() as conn:
        from sib.tests.conftest import _create_all_compatible

        await conn.run_sync(_create_all_compatible)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_scope():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(start_module, "get_session", session_scope)
    monkeypatch.setattr(
        MembershipService,
        "check_user_membership",
        AsyncMock(return_value=(True, None)),
    )

    async with factory.begin() as session:
        session.add(User(id=INVITER_ID, first_name="دعوت‌کننده"))
    yield factory
    await engine.dispose()


async def _load_user(factory, user_id):
    async with factory() as session:
        user = await session.get(User, user_id)
        if user is not None:
            session.expunge(user)
        return user


async def _balance(factory, user_id):
    async with factory() as session:
        return await AppleWalletService.get_balance(session, user_id)


async def _referral_ledger(factory):
    async with factory() as session:
        rows = (
            await session.scalars(
                select(AppleTransaction).where(
                    AppleTransaction.ref_type == "referral"
                )
            )
        ).all()
        return [(row.user_id, row.amount, row.reason, row.idempotency_key) for row in rows]


def _private_start_message(user_id=INVITEE_ID, payload=None):
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=user_id, type=ChatType.PRIVATE)
    msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name="تازه‌وارد")
    msg.text = f"/start {payload}" if payload else "/start"
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _referral_command(inviter_id=INVITER_ID):
    """The ``/start ref_<inviter id>`` payload aiogram would parse."""
    return CommandObject(
        prefix="/", command="start", mention=None, args=f"ref_{inviter_id}"
    )


def _group_start_message(user_id=INVITEE_ID):
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="گروه دوستان")
    msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name="تازه‌وارد")
    msg.text = "/start"
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


async def _register_invitee_with_referral(bot):
    command = _referral_command()
    await handle_start_deep_link(_private_start_message(), bot, command)


# ---------------------------------------------------------------------------
# Deep-link handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_link_saves_referrer(ref_db):
    bot = AsyncMock()
    await _register_invitee_with_referral(bot)

    invitee = await _load_user(ref_db, INVITEE_ID)
    assert invitee is not None
    assert invitee.referred_by_id == INVITER_ID
    # REF2: the attribution and the payment land together, right on /start.
    assert invitee.referral_rewarded is True
    assert await _balance(ref_db, INVITER_ID) == INVITER_REWARD_APPLES
    assert await _balance(ref_db, INVITEE_ID) == INVITEE_REWARD_APPLES
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_self_referral_blocked(ref_db):
    bot = AsyncMock()
    command = CommandObject(
        prefix="/", command="start", mention=None, args=f"ref_{INVITEE_ID}"
    )
    await handle_start_deep_link(_private_start_message(), bot, command)

    invitee = await _load_user(ref_db, INVITEE_ID)
    assert invitee is not None
    assert invitee.referred_by_id is None

    # The guard also holds when the would-be referrer row exists.
    async with ref_db() as session:
        assert (
            await ReferralService.resolve_referrer_id(
                session,
                new_user_id=INVITER_ID,
                candidate_referrer_id=INVITER_ID,
            )
            is None
        )


@pytest.mark.parametrize(
    "payload",
    ["ref_999999", "ref_abc", "ref_", "ref_-5", "ref_0", "something_else"],
)
@pytest.mark.asyncio
async def test_unknown_or_malformed_referrer_ignored(ref_db, payload):
    bot = AsyncMock()
    command = CommandObject(prefix="/", command="start", mention=None, args=payload)
    await handle_start_deep_link(_private_start_message(), bot, command)

    invitee = await _load_user(ref_db, INVITEE_ID)
    assert invitee is not None
    assert invitee.referred_by_id is None
    assert ReferralService.parse_referrer_id(payload) in (None, 999999)


@pytest.mark.asyncio
async def test_existing_user_deep_link_is_ignored(ref_db):
    bot = AsyncMock()
    # Register first without any referral...
    await handle_start(_private_start_message(), bot)
    # ... then open someone else's referral link: attribution must not appear.
    await _register_invitee_with_referral(bot)

    invitee = await _load_user(ref_db, INVITEE_ID)
    assert invitee.referred_by_id is None


def test_deep_link_start_is_routed_before_plain_start():
    """``CommandStart(deep_link=True)`` must win for payloads, while the
    plain ``/start`` keeps its own handler right behind it."""
    start_handlers = [
        handler
        for handler in start_module.router.message.handlers
        if getattr(handler.callback, "__name__", "")
        in ("handle_start_deep_link", "handle_start")
    ]
    assert [h.callback.__name__ for h in start_handlers] == [
        "handle_start_deep_link",
        "handle_start",
    ]
    deep_link_flags = [
        getattr(f.callback, "deep_link", None)
        for h in start_handlers
        for f in h.filters
    ]
    assert deep_link_flags == [True, None]


# ---------------------------------------------------------------------------
# Reward
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reward_given_immediately_on_start(ref_db):
    """REF2: the invitee's very first /start pays both sides at once."""
    bot = AsyncMock()
    await _register_invitee_with_referral(bot)

    # No group activity, no second message - the wallets are already paid.
    assert await _balance(ref_db, INVITER_ID) == INVITER_REWARD_APPLES == 100
    assert await _balance(ref_db, INVITEE_ID) == INVITEE_REWARD_APPLES == 50

    invitee = await _load_user(ref_db, INVITEE_ID)
    assert invitee.referral_rewarded is True

    bot.send_message.assert_awaited_once_with(
        chat_id=INVITER_ID,
        text="🎉 یکی از دوستان شما با لینک شما وارد بازی شد! ۱۰۰ سیب هدیه گرفتید.",
        parse_mode=None,
    )
    assert INVITER_NOTIFICATION_TEXT == bot.send_message.await_args.kwargs["text"]
    assert sorted(await _referral_ledger(ref_db)) == sorted(
        [
            (INVITER_ID, 100, "referral_inviter", f"referral_inviter:{INVITEE_ID}"),
            (INVITEE_ID, 50, "referral_invitee", f"referral_invitee:{INVITEE_ID}"),
        ]
    )


@pytest.mark.asyncio
async def test_invitee_is_told_about_the_bonus(ref_db):
    """The invitee's /start reply mentions the 50-apple bonus (Persian)."""
    bot = AsyncMock()
    welcome = _private_start_message(user_id=SECOND_INVITEE_ID)
    await handle_start_deep_link(welcome, bot, _referral_command())

    text = welcome.answer.await_args.args[0]
    assert INVITEE_BONUS_TEXT in text
    assert "۵۰ سیب" in text
    assert await _balance(ref_db, SECOND_INVITEE_ID) == INVITEE_REWARD_APPLES


@pytest.mark.asyncio
async def test_bonus_is_paid_behind_the_membership_gate(ref_db, monkeypatch):
    """The channel gate does not delay the referral bonus."""
    monkeypatch.setattr(
        MembershipService,
        "check_user_membership",
        AsyncMock(return_value=(False, None)),
    )
    bot = AsyncMock()
    gated = _private_start_message(user_id=SECOND_INVITEE_ID)
    await handle_start_deep_link(gated, bot, _referral_command())

    assert INVITEE_BONUS_TEXT in gated.answer.await_args.args[0]
    assert await _balance(ref_db, SECOND_INVITEE_ID) == INVITEE_REWARD_APPLES
    assert await _balance(ref_db, INVITER_ID) == INVITER_REWARD_APPLES


@pytest.mark.asyncio
async def test_reward_given_only_once(ref_db):
    """Starting the bot again (private or in a group) never pays twice."""
    bot = AsyncMock()
    await _register_invitee_with_referral(bot)

    # A second /start in private, a /start in a group and even a direct
    # service retry must not pay a second time.
    await handle_start(_private_start_message(), bot)
    await handle_start(_group_start_message(), bot)
    async with ref_db.begin() as session:
        assert await ReferralService.grant_reward_if_due(session, INVITEE_ID) is None

    assert await _balance(ref_db, INVITER_ID) == 100
    assert await _balance(ref_db, INVITEE_ID) == 50
    assert len(await _referral_ledger(ref_db)) == 2
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_start_alone_also_pays(ref_db):
    """A referral deep link opened in a group pays the same way."""
    bot = AsyncMock()
    command = _referral_command()
    group_msg = _group_start_message()
    group_msg.text = "/start ref_1001"
    await handle_start_deep_link(group_msg, bot, command)

    group_msg.reply.assert_awaited_once()
    assert await _balance(ref_db, INVITER_ID) == 100
    assert await _balance(ref_db, INVITEE_ID) == 50
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_referral_leaderboard_counts_new_referrals(ref_db):
    """The contest board counts the referrals that were paid on /start."""
    bot = AsyncMock()
    await _register_invitee_with_referral(bot)
    await handle_start_deep_link(
        _private_start_message(user_id=SECOND_INVITEE_ID),
        bot,
        _referral_command(),
    )

    async with ref_db() as session:
        assert await ReferralService.count_referrals(session, INVITER_ID) == 2
        board = await ReferralService.leaderboard(session)
        assert await ReferralService.count_referrals(session, INVITEE_ID) == 0

    assert board == [(INVITER_ID, 2)]

    # Both referrals really paid out (the board counts paid referrals).
    assert await _balance(ref_db, INVITER_ID) == 2 * INVITER_REWARD_APPLES
    for invitee_id in (INVITEE_ID, SECOND_INVITEE_ID):
        invitee = await _load_user(ref_db, invitee_id)
        assert invitee.referral_rewarded is True


def test_group_activity_middleware_is_removed():
    """REF2 pays on /start - the group-activity middleware is gone."""
    repo_root = Path(__file__).resolve().parents[1]
    assert not (repo_root / "middleware" / "referral_reward.py").exists()
    assert "referral_reward" not in (repo_root / "bot.py").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Invite UI
# ---------------------------------------------------------------------------


def test_profile_has_invite_friends_button():
    buttons = [
        button
        for row in get_profile_keyboard().inline_keyboard
        for button in row
    ]
    invite = next(b for b in buttons if b.callback_data == "referral_invite")
    assert invite.text == "🎁 دعوت دوستان"
    assert invite.style == "primary"


def test_invite_screen_shows_personal_deep_link_and_share_button():
    text, keyboard = start_module._referral_invite_screen(4242)
    link = f"https://t.me/{settings.BOT_USERNAME}?start=ref_4242"

    assert "👋 دوستانت رو به سیب دعوت کن!" in text
    assert "۱۰۰ سیب هدیه می‌گیری" in text
    assert "لینک اختصاصی شما:" in text
    assert link in text

    buttons = [b for row in keyboard.inline_keyboard for b in row]
    share = next(b for b in buttons if b.text == "🔗 اشتراک‌گذاری لینک")
    assert share.style == "primary"
    assert share.url.startswith("https://t.me/share/url?url=")
    assert quote(link, safe="") in share.url
    assert any(b.callback_data == "apple_profile" for b in buttons)


@pytest.mark.asyncio
async def test_invite_callback_sends_screen(ref_db, monkeypatch):
    edit = AsyncMock()
    monkeypatch.setattr(start_module, "safe_edit_callback_message", edit)
    monkeypatch.setattr(start_module, "_claim_panel", AsyncMock())

    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = TelegramUser(id=7777, is_bot=False, first_name="مهمان")
    callback.data = "referral_invite"
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.message_id = 10
    callback.message.chat = Chat(id=7777, type=ChatType.PRIVATE)

    await start_module.handle_referral_invite(callback, AsyncMock())

    callback.answer.assert_awaited()
    text = edit.await_args.args[2]
    assert f"https://t.me/{settings.BOT_USERNAME}?start=ref_7777" in text
    assert edit.await_args.kwargs["parse_mode"] is None


def test_referral_link_builders():
    assert (
        ReferralService.build_referral_link("@sib_game_bot", 42)
        == "https://t.me/sib_game_bot?start=ref_42"
    )
    assert ReferralService.parse_referrer_id("ref_42") == 42
    assert ReferralService.parse_referrer_id("  ref_42  ") == 42
    assert ReferralService.parse_referrer_id(None) is None
    assert ReferralService.parse_referrer_id("ref_4_2") is None


# ---------------------------------------------------------------------------
# Migration 051
# ---------------------------------------------------------------------------


def test_051_referrals_migration_is_reversible():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "database/migrations/versions/051_referrals.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_051_referrals", migration_path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "051_referrals"
    assert migration.down_revision == "050_apple_clash"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE users (id BIGINT PRIMARY KEY, first_name VARCHAR(255))"
        )
        connection.exec_driver_sql("INSERT INTO users (id, first_name) VALUES (7, 'x')")

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        inspector = inspect(connection)
        columns = {c["name"]: c for c in inspector.get_columns("users")}
        assert columns["referred_by_id"]["nullable"] is True
        assert "BIGINT" in str(columns["referred_by_id"]["type"]).upper()
        assert columns["referral_rewarded"]["nullable"] is False
        fks = inspector.get_foreign_keys("users")
        assert any(
            fk["referred_table"] == "users"
            and fk["constrained_columns"] == ["referred_by_id"]
            for fk in fks
        )
        assert "ix_users_referred_by_id" in {
            ix["name"] for ix in inspector.get_indexes("users")
        }
        # Existing players default to "not rewarded", and a referral can be stored.
        assert connection.exec_driver_sql(
            "SELECT referral_rewarded FROM users WHERE id = 7"
        ).scalar() in (0, False)
        connection.exec_driver_sql(
            "INSERT INTO users (id, first_name, referred_by_id) VALUES (8, 'y', 7)"
        )

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        names = {c["name"] for c in inspect(connection).get_columns("users")}
        assert "referred_by_id" not in names
        assert "referral_rewarded" not in names
    engine.dispose()
