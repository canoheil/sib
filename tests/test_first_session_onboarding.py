"""First-session onboarding: the starter pack and the real /start menu.

Background (``docs/growth-diagnosis-fa.md``): a brand-new player used to be
created with nothing — no card, no Apples — and the private /start offered a
single button («افزودن سیب به گروه») that most players cannot press. The
first card, the object the whole game is about, was about three hours of
grinding away.

These tests pin the fix:

* one released shop card is granted and immediately selected as the Main Card;
* ``STARTER_APPLES`` are credited through the ledger;
* the pack is paid **exactly once** per account (the ledger key is the marker),
  so leaving/re-entering a group cannot farm it;
* a player who already owns the starter character still gets the Apples and
  never crashes;
* a deployment without characters still pays the Apples;
* the private /start keyboard is the real menu, with add-to-group LAST;
* the channel gate is soft: a non-member still gets the menu and the pack;
* a group-first /start also pays, so group members end up with a Main Card.

The database is an isolated in-memory SQLite engine shared by one connection
(no write locks, nothing leaks between tests), because ``/start`` opens inner
sessions of its own while the test also holds one.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from aiogram.types import Chat as TelegramChat, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import sib.handlers.start as start_module
from sib.database.models import (
    AppleTransaction,
    Character,
    PlayerCard,
    ReleaseStatus,
    User,
)
from sib.handlers.start import handle_start
from sib.keyboards.common import get_private_start_keyboard
from sib.services.apple_wallet import AppleWalletService
from sib.services.bot_commands import PUBLIC_COMMANDS, register_bot_commands
from sib.services.membership.checker import MembershipService
from sib.services.onboarding import (
    PREFERRED_STARTER_CARD_CODE,
    STARTER_APPLES,
    STARTER_LEDGER_KEY,
    STARTER_LEDGER_REASON,
    OnboardingService,
)

USER_ID = 900900
GROUP_ID = -1001234567
ADD_TO_GROUP_LABEL = "➕ افزودن سیب به گروه"


@pytest_asyncio.fixture
async def onboarding_db(monkeypatch):
    """Isolated in-memory database; ``/start`` writes through the same engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
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
    # Verified member by default; individual tests override the gate.
    monkeypatch.setattr(
        MembershipService,
        "check_user_membership",
        AsyncMock(return_value=(True, None)),
    )
    yield factory
    await engine.dispose()


def _flatten(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def _seed_user(factory, user_id: int = USER_ID, first_name: str = "سارا") -> None:
    async with factory.begin() as session:
        session.add(User(id=user_id, first_name=first_name, is_bot=False, is_active=True))


async def _seed_character(
    factory,
    *,
    code: str = PREFERRED_STARTER_CARD_CODE,
    name_fa: str = "سیب سرخ",
    shop_enabled: bool = True,
    is_active: bool = True,
    release_status: str = ReleaseStatus.RELEASED,
    power: int = 22,
    speed: int = 20,
    intelligence: int = 18,
) -> int:
    async with factory.begin() as session:
        character = Character(
            code=code,
            name_fa=name_fa,
            power=power,
            speed=speed,
            intelligence=intelligence,
            shop_enabled=shop_enabled,
            is_active=is_active,
            release_status=release_status,
        )
        session.add(character)
        await session.flush()
        return character.id


async def _grant(factory, user_id: int = USER_ID):
    async with factory() as session:
        pack = await OnboardingService.grant_starter_pack(session, user_id)
        await session.commit()
        return pack


async def _balance(factory, user_id: int = USER_ID) -> int:
    async with factory() as session:
        return await AppleWalletService.get_balance(session, user_id)


async def _cards(factory, user_id: int = USER_ID):
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(PlayerCard).where(PlayerCard.user_id == user_id)
                )
            ).all()
        )


def _private_message(user_id: int = USER_ID, first_name: str = "سارا"):
    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=user_id, type="private")
    msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name=first_name)
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


async def _private_start(user_id: int = USER_ID, first_name: str = "سارا"):
    msg = _private_message(user_id=user_id, first_name=first_name)
    await handle_start(msg, AsyncMock())
    return msg


# --------------------------------------------------------------------------- #
# The pack itself
# --------------------------------------------------------------------------- #
async def test_starter_pack_grants_card_and_apples(onboarding_db):
    await _seed_user(onboarding_db)
    await _seed_character(onboarding_db)

    pack = await _grant(onboarding_db)

    assert pack.granted is True
    assert pack.apples == STARTER_APPLES
    assert pack.card_name == "سیب سرخ"
    assert pack.balance == STARTER_APPLES

    cards = await _cards(onboarding_db)
    assert len(cards) == 1
    # Level 1 and already the Main Card: battles, duels and invasions read it.
    assert cards[0].level == 1
    assert cards[0].is_favorite is True
    assert await _balance(onboarding_db) == STARTER_APPLES


async def test_starter_pack_is_paid_exactly_once(onboarding_db):
    await _seed_user(onboarding_db)
    await _seed_character(onboarding_db)

    first = await _grant(onboarding_db)
    second = await _grant(onboarding_db)
    third = await _grant(onboarding_db)

    assert first.granted is True
    assert second.granted is False
    assert third.granted is False
    assert await _balance(onboarding_db) == STARTER_APPLES
    assert len(await _cards(onboarding_db)) == 1


async def test_starter_pack_ledger_row_is_the_marker(onboarding_db):
    """The durable "already granted" marker is the ledger key, not new schema."""
    await _seed_user(onboarding_db)
    await _seed_character(onboarding_db)
    await _grant(onboarding_db)

    async with onboarding_db() as session:
        row = await session.scalar(
            select(AppleTransaction).where(
                AppleTransaction.idempotency_key
                == STARTER_LEDGER_KEY.format(user_id=USER_ID)
            )
        )
    assert row is not None
    assert row.reason == STARTER_LEDGER_REASON
    assert row.amount == STARTER_APPLES


async def test_starter_pack_skips_card_already_owned(onboarding_db):
    """A player who already owns the starter character keeps one card."""
    await _seed_user(onboarding_db)
    character_id = await _seed_character(onboarding_db)
    async with onboarding_db.begin() as session:
        session.add(PlayerCard(user_id=USER_ID, character_id=character_id, level=3))

    pack = await _grant(onboarding_db)

    assert pack.granted is True
    assert pack.card_name is None  # nothing new to announce
    assert await _balance(onboarding_db) == STARTER_APPLES
    cards = await _cards(onboarding_db)
    assert len(cards) == 1
    assert cards[0].level == 3  # the owned card is untouched


async def test_starter_pack_without_characters_still_pays_apples(onboarding_db):
    """A deployment with an empty roster must not break /start."""
    await _seed_user(onboarding_db)

    pack = await _grant(onboarding_db)

    assert pack.granted is True
    assert pack.card_name is None
    assert await _balance(onboarding_db) == STARTER_APPLES
    assert await _cards(onboarding_db) == []


async def test_starter_pack_falls_back_to_cheapest_shop_character(onboarding_db):
    """Retired/unreleased cards are never gifted; a shop-eligible one is."""
    await _seed_user(onboarding_db)
    await _seed_character(
        onboarding_db,
        code="retired_card",
        name_fa="کارت بازنشسته",
        shop_enabled=False,
    )
    fallback_id = await _seed_character(
        onboarding_db,
        code="sib_sabz",
        name_fa="سیب سبز",
        power=16,
        speed=18,
        intelligence=26,
    )

    pack = await _grant(onboarding_db)

    assert pack.card_name == "سیب سبز"
    cards = await _cards(onboarding_db)
    assert [card.character_id for card in cards] == [fallback_id]


async def test_starter_pack_never_gifts_an_unreleased_card(onboarding_db):
    await _seed_user(onboarding_db)
    await _seed_character(
        onboarding_db,
        code="secret_card",
        name_fa="کارت منتشرنشده",
        release_status="UNRELEASED",
    )

    pack = await _grant(onboarding_db)

    assert pack.granted is True
    assert pack.card_name is None
    assert await _cards(onboarding_db) == []
    assert await _balance(onboarding_db) == STARTER_APPLES


# --------------------------------------------------------------------------- #
# The private /start screen
# --------------------------------------------------------------------------- #
async def test_private_start_pays_the_starter_pack(onboarding_db):
    await _seed_character(onboarding_db)

    msg = await _private_start()

    text = msg.answer.call_args[0][0]
    kwargs = msg.answer.call_args[1]
    assert "هدیهٔ شروع" in text
    assert "سیب سرخ" in text
    assert "۱۰۰" in text  # Persian digits only
    assert str(STARTER_APPLES) not in text

    callbacks = [
        b.callback_data for b in _flatten(kwargs["reply_markup"]) if b.callback_data
    ]
    assert "apple_chest" in callbacks
    assert await _balance(onboarding_db) == STARTER_APPLES
    assert len(await _cards(onboarding_db)) == 1


async def test_private_start_keyboard_is_the_real_menu(onboarding_db):
    keyboard = get_private_start_keyboard("sib_game_bot")
    buttons = _flatten(keyboard)
    callbacks = [b.callback_data for b in buttons if b.callback_data]

    assert len(buttons) > 1  # NOT the old single funnel button
    for expected in (
        "apple_chest",
        "apple_garden",
        "apple_cards",
        "apple_shop",
        "apple_profile",
        "help_menu",
    ):
        assert expected in callbacks
    # Add-to-group is still there, but it is the LAST row: the goal, not a wall.
    assert keyboard.inline_keyboard[-1][0].text == ADD_TO_GROUP_LABEL
    assert keyboard.inline_keyboard[-1][0].url == (
        "https://t.me/sib_game_bot?startgroup=true"
    )
    # No channel row unless the (soft) gate asks for one.
    assert all("عضویت در کانال" not in b.text for b in buttons)


async def test_second_start_does_not_pay_twice(onboarding_db):
    await _seed_character(onboarding_db)

    await _private_start()
    msg = await _private_start()

    text = msg.answer.call_args[0][0]
    assert "هدیهٔ شروع" not in text  # nothing new to announce
    assert await _balance(onboarding_db) == STARTER_APPLES
    assert len(await _cards(onboarding_db)) == 1


async def test_group_first_start_also_pays_the_pack(onboarding_db):
    """A group-first player ends up with a Main Card (invasion counts it)."""
    await _seed_character(onboarding_db)

    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=GROUP_ID, type="supergroup", title="مدافعان")
    msg.from_user = TelegramUser(id=USER_ID, is_bot=False, first_name="کیان")
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()

    await handle_start(msg, AsyncMock())

    cards = await _cards(onboarding_db)
    assert len(cards) == 1
    assert cards[0].is_favorite is True
    assert await _balance(onboarding_db) == STARTER_APPLES


# --------------------------------------------------------------------------- #
# Soft membership gate
# --------------------------------------------------------------------------- #
async def test_non_member_still_gets_menu_and_pack(onboarding_db, monkeypatch):
    """The channel gate never blocks the first session any more."""
    await _seed_character(onboarding_db)

    missing = MagicMock()
    missing.invite_link = "https://t.me/+sponsor"
    monkeypatch.setattr(
        MembershipService,
        "check_user_membership",
        AsyncMock(return_value=(False, missing)),
    )

    msg = await _private_start()

    text = msg.answer.call_args[0][0]
    kwargs = msg.answer.call_args[1]
    labels = [b.text for b in _flatten(kwargs["reply_markup"])]

    assert "🎁 صندوق روزانه" in labels  # the menu is there
    assert "📢 عضویت در کانال سیب" in labels  # the join row is offered
    assert "✅ بررسی" in labels  # … and can be verified without leaving
    callbacks = [
        b.callback_data for b in _flatten(kwargs["reply_markup"]) if b.callback_data
    ]
    assert "check_membership" in callbacks
    assert "کانال رسمی" in text
    assert "هدیهٔ شروع" in text
    # The pack landed even though the gate is unanswered.
    assert await _balance(onboarding_db) == STARTER_APPLES
    assert len(await _cards(onboarding_db)) == 1


# --------------------------------------------------------------------------- #
# The published command menu
# --------------------------------------------------------------------------- #
async def test_public_commands_are_registered():
    bot = AsyncMock()
    assert await register_bot_commands(bot) is True

    sent = bot.set_my_commands.await_args.args[0]
    assert [(c.command, c.description) for c in sent] == list(PUBLIC_COMMANDS)
    assert len(sent) == len(PUBLIC_COMMANDS)
    # Every listed command is a real player command, never an admin one.
    names = [name for name, _description in PUBLIC_COMMANDS]
    for admin_only in ("admin", "users", "broadcast", "setasset"):
        assert admin_only not in names
    for name in names:
        assert name.islower() and " " not in name


async def test_command_menu_failure_never_blocks_startup():
    bot = AsyncMock()
    bot.set_my_commands.side_effect = RuntimeError("telegram is down")
    assert await register_bot_commands(bot) is False
