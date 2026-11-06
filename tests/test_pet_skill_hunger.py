"""Pet System phase 6: skill upgrades (Diamond Apples) and hunger/satiety.

Three layers are covered:

* :func:`sib.services.pet_service.get_satiety_status` / ``is_skill_active``
  own the hunger rule (a pet not fed for >24h is «گشنه» and its skill is off);
* :func:`sib.services.pet_service.upgrade_skill` owns the upgrade rules
  (adult only, skill cap, the Diamond-Apple price) and ``feed_pet`` now
  satiety-feeds adults without granting XP;
* :mod:`sib.handlers.pets` only renders the result — the «💎 ارتقای اسکیل»
  button, the 🔴 disabled-skill line and the satiety line of the profile.

No new migration: every config key and column already exists (head is
``079_disease_daily_announcement``).
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    SystemConfig,
    User,
    UserItem,
    UserPet,
)
from sib.handlers import pets
from sib.services import pet_service
from sib.services.items import ItemService
from sib.services.sib_claim import fa_digits

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
DIAMOND = pet_service.SKILL_UPGRADE_ITEM  # "apple_diamond"
COST = pet_service.SKILL_UPGRADE_COST_DEFAULT  # 10


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _clean_pending_hunts():
    pet_service._PENDING_HUNT_ATTEMPTS.clear()
    yield
    pet_service._PENDING_HUNT_ATTEMPTS.clear()


@pytest_asyncio.fixture
async def engine():
    """In-memory SQLite with only the tables this feature touches."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        User.__table__,
        SystemConfig.__table__,
        UserPet.__table__,
        UserItem.__table__,
        AppleWallet.__table__,
        AppleTransaction.__table__,
    ]
    narrowed = []
    for table in tables:
        for column in table.columns:
            if column.primary_key and type(column.type).__name__ == "BigInteger":
                narrowed.append((column, column.type))
                column.type = sa.Integer()
    try:
        async with eng.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: User.metadata.create_all(
                    sync_connection, tables=tables
                )
            )
    finally:
        for column, original in narrowed:
            column.type = original
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory.begin() as seed:
        seed.add(User(id=1, first_name="pet owner"))
        seed.add(User(id=2, first_name="someone else"))
    async with factory() as db:
        yield db


@pytest_asyncio.fixture
async def db(engine, monkeypatch):
    """Session factory shared with the handler through a patched get_session."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def get_session():
        async with factory.begin() as db_session:
            yield db_session

    monkeypatch.setattr(pets, "get_session", get_session)
    async with factory.begin() as db_session:
        db_session.add(User(id=1, first_name="pet owner"))
        db_session.add(User(id=2, first_name="someone else"))
    yield factory


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _seed_pet(
    session,
    *,
    user_id: int = 1,
    pet_type: str = "cat",
    is_adult: bool = True,
    custom_name: str | None = "ملوس",
    xp: int = 500,
    skill_level: int = 1,
    last_fed_at: datetime | None = None,
) -> UserPet:
    pet = UserPet(
        user_id=user_id,
        pet_type=pet_type,
        is_adult=is_adult,
        custom_name=custom_name,
        xp=xp,
        skill_level=skill_level,
        last_fed_at=last_fed_at,
    )
    session.add(pet)
    await session.flush()
    return pet


async def _grant_diamonds(session, user_id: int = 1, quantity: int = COST) -> None:
    await ItemService.grant(session, user_id, DIAMOND, quantity)


async def _diamonds(session, user_id: int = 1) -> int:
    return await ItemService.get_total(session, user_id, DIAMOND)


async def _grant_food(session, user_id: int = 1, quantity: int = 1) -> None:
    await ItemService.grant(session, user_id, pet_service.PET_FOOD_ITEM, quantity)


async def _config(session, key: str, value) -> None:
    session.add(SystemConfig(key=key, value=str(value)))
    await session.flush()


def _callback(data: str, *, clicker_id: int = 1, message_id: int = 5, with_photo=True):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=clicker_id, is_bot=False),
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=1),
            photo=[SimpleNamespace(file_id="f")] if with_photo else None,
            answer=AsyncMock(),
            answer_photo=AsyncMock(),
        ),
    )


def _bot():
    return SimpleNamespace(
        edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(return_value=True),
        send_message=AsyncMock(),
    )


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# get_satiety_status — the hunger rule
# ---------------------------------------------------------------------------

def test_satiety_never_fed_is_hungry():
    assert pet_service.get_satiety_status(None, now=NOW) == pet_service.SATIETY_HUNGRY


@pytest.mark.parametrize(
    "age, expected",
    [
        (timedelta(0), "سیر"),
        (timedelta(hours=1), "سیر"),
        (timedelta(hours=23, minutes=59), "سیر"),
        (timedelta(hours=24), "سیر"),  # exactly 24h is still full
        (timedelta(hours=24, seconds=1), "گشنه"),  # MORE than 24h
        (timedelta(hours=30), "گشنه"),
        (timedelta(days=3), "گشنه"),
    ],
)
def test_satiety_status_by_age(age, expected):
    last_fed = NOW - age
    assert pet_service.get_satiety_status(last_fed, now=NOW) == expected


def test_satiety_status_accepts_naive_database_datetimes():
    # SQLite drops the tzinfo; the helper must still read it as UTC.
    naive = (NOW - timedelta(hours=30)).replace(tzinfo=None)
    assert pet_service.get_satiety_status(naive, now=NOW) == "گشنه"
    naive_full = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    assert pet_service.get_satiety_status(naive_full, now=NOW) == "سیر"


def test_is_skill_active_requires_adult_and_full():
    full = NOW - timedelta(hours=2)
    hungry = NOW - timedelta(hours=30)
    assert pet_service.is_skill_active(
        SimpleNamespace(is_adult=True, last_fed_at=full), now=NOW
    ) is True
    assert pet_service.is_skill_active(
        SimpleNamespace(is_adult=True, last_fed_at=hungry), now=NOW
    ) is False
    # A baby's skill is locked by age even when it is well fed.
    assert pet_service.is_skill_active(
        SimpleNamespace(is_adult=False, last_fed_at=full), now=NOW
    ) is False
    assert pet_service.is_skill_active(
        SimpleNamespace(is_adult=True, last_fed_at=None), now=NOW
    ) is False


# ---------------------------------------------------------------------------
# upgrade_skill — the service rules
# ---------------------------------------------------------------------------

async def test_upgrade_skill_without_diamonds(session):
    pet = await _seed_pet(session, skill_level=1)

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "no_diamonds"
    assert result["cost"] == COST
    assert result["owned"] == 0
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 1


async def test_upgrade_skill_with_insufficient_diamonds(session):
    pet = await _seed_pet(session, skill_level=1)
    await _grant_diamonds(session, quantity=COST - 1)  # 9, needs 10

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "no_diamonds"
    assert await _diamonds(session) == COST - 1  # nothing spent
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 1


async def test_upgrade_skill_success_consumes_diamonds_and_raises_level(session):
    pet = await _seed_pet(session, skill_level=1)
    await _grant_diamonds(session, quantity=COST + 3)  # 13

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "upgraded"
    assert result["skill_level"] == 2
    assert result["cost"] == COST
    assert result["remaining_diamonds"] == 3
    assert await _diamonds(session) == 3
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 2


async def test_upgrade_skill_can_reach_the_cap_then_stops(session):
    pet = await _seed_pet(session, skill_level=1)
    await _grant_diamonds(session, quantity=COST * 5)

    first = await pet_service.upgrade_skill(session, 1, pet.id)
    second = await pet_service.upgrade_skill(session, 1, pet.id)
    third = await pet_service.upgrade_skill(session, 1, pet.id)

    assert first["skill_level"] == 2
    assert second["skill_level"] == 3
    assert third["status"] == "max_level"  # capped at 3
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 3
    # Only two upgrades were paid for (1 -> 2 -> 3).
    assert await _diamonds(session) == COST * 5 - COST * 2


async def test_upgrade_skill_at_max_level_spends_nothing(session):
    pet = await _seed_pet(session, skill_level=3)
    await _grant_diamonds(session, quantity=COST)

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "max_level"
    assert await _diamonds(session) == COST
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 3


async def test_upgrade_skill_refuses_a_baby(session):
    pet = await _seed_pet(session, is_adult=False, xp=0, skill_level=1)
    await _grant_diamonds(session, quantity=COST)

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "not_adult"
    assert await _diamonds(session) == COST
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 1


async def test_upgrade_skill_refuses_another_players_pet(session):
    pet = await _seed_pet(session, user_id=2, skill_level=1)
    await _grant_diamonds(session, user_id=1, quantity=COST)

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "not_found"
    assert await _diamonds(session, 1) == COST
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.skill_level == 1


@pytest.mark.parametrize("bad_id", [9999, None, "abc"])
async def test_upgrade_skill_refuses_a_bad_pet_id(session, bad_id):
    await _grant_diamonds(session, quantity=COST)
    result = await pet_service.upgrade_skill(session, 1, bad_id)
    assert result["status"] == "not_found"
    assert await _diamonds(session) == COST


async def test_upgrade_skill_cost_is_configurable(session):
    await _config(session, "pet_skill_upgrade_diamond_cost", 4)
    pet = await _seed_pet(session, skill_level=1)
    await _grant_diamonds(session, quantity=4)

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "upgraded"
    assert result["cost"] == 4
    assert await _diamonds(session) == 0


async def test_upgrade_skill_max_level_is_configurable(session):
    await _config(session, "pet_skill_max_level", 2)
    pet = await _seed_pet(session, skill_level=2)
    await _grant_diamonds(session, quantity=COST)

    result = await pet_service.upgrade_skill(session, 1, pet.id)

    assert result["status"] == "max_level"
    assert await _diamonds(session) == COST


# ---------------------------------------------------------------------------
# Feeding an adult — satiety, not XP
# ---------------------------------------------------------------------------

async def test_feeding_an_adult_resets_hunger_but_not_xp(session):
    pet = await _seed_pet(
        session, is_adult=True, xp=500, last_fed_at=NOW - timedelta(hours=30)
    )
    await _grant_food(session)
    assert pet_service.get_satiety_status(pet.last_fed_at, now=NOW) == "گشنه"

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "satiated"
    assert result["xp_added"] == 0
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 500  # never beyond the adult threshold
    assert stored.last_fed_at.replace(tzinfo=timezone.utc) == NOW
    assert pet_service.get_satiety_status(stored.last_fed_at, now=NOW) == "سیر"
    assert pet_service.is_skill_active(stored, now=NOW) is True


async def test_feeding_an_adult_still_respects_the_feed_cooldown(session):
    pet = await _seed_pet(
        session, is_adult=True, xp=500, last_fed_at=NOW - timedelta(minutes=10)
    )
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "too_soon"
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 500


# ---------------------------------------------------------------------------
# The profile UI — satiety line and disabled skill
# ---------------------------------------------------------------------------

def test_hungry_adult_profile_disables_the_skill():
    pet = SimpleNamespace(
        pet_type="cat", custom_name="ملوس", xp=500, is_adult=True,
        skill_level=2, last_fed_at=NOW - timedelta(hours=30),
    )
    caption = pets.pet_profile_caption(pet, 500)

    assert pets.SATIETY_HUNGRY_LINE in caption
    assert "🔴" in pets.pet_skill_line(pet)
    assert "✨ اسکیل: چشم شانس - لول ۲" in caption  # level still shown


def test_full_adult_profile_keeps_the_skill_active():
    pet = SimpleNamespace(
        pet_type="cat", custom_name="ملوس", xp=500, is_adult=True,
        skill_level=2, last_fed_at=NOW - timedelta(hours=2),
    )
    caption = pets.pet_profile_caption(pet, 500)

    assert pets.SATIETY_FULL_LINE in caption
    assert "🔴" not in pets.pet_skill_line(pet)


def test_baby_profile_shows_plain_satiety_and_locked_skill():
    hungry_baby = SimpleNamespace(
        pet_type="dog", custom_name="رکس", xp=0, is_adult=False,
        skill_level=1, last_fed_at=None,
    )
    caption = pets.pet_profile_caption(hungry_baby, 500)
    assert "🍖 وضعیت سیری: گشنه" in caption
    assert "🔒 اسکیل: پارس هشدار" in caption
    # A baby's skill is locked by age, not hunger: no 🔴 disabled marker.
    assert "🔴" not in caption


@pytest.mark.parametrize("level, shows_upgrade", [(1, True), (2, True), (3, False)])
def test_upgrade_button_visibility(level, shows_upgrade):
    pet = SimpleNamespace(id=7, is_adult=True, has_renamed=True, skill_level=level)
    keyboard = pets.pet_profile_keyboard(pet)
    callbacks = [b.callback_data for b in _buttons(keyboard)]
    assert ("pet_skill_up:7" in callbacks) is shows_upgrade
    # Feeding is always available (hunger applies to adults too).
    assert "pet_feed:7" in callbacks


def test_upgrade_button_hidden_for_a_baby():
    pet = SimpleNamespace(id=7, is_adult=False, has_renamed=False, skill_level=1)
    callbacks = [b.callback_data for b in _buttons(pets.pet_profile_keyboard(pet))]
    assert "pet_skill_up:7" not in callbacks
    assert "pet_feed:7" in callbacks


def test_upgrade_button_is_primary_style():
    pet = SimpleNamespace(id=7, is_adult=True, has_renamed=True, skill_level=1)
    by_callback = {b.callback_data: b for b in _buttons(pets.pet_profile_keyboard(pet))}
    button = by_callback["pet_skill_up:7"]
    assert button.text == pets.SKILL_UP_BUTTON_TEXT
    assert button.style == "primary"


# ---------------------------------------------------------------------------
# Handlers — the skill upgrade callback
# ---------------------------------------------------------------------------

async def test_skill_up_callback_without_diamonds(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=500, is_adult=True, skill_level=1)
        session.add(pet)
        await session.flush()
        pet_id = pet.id

    callback = _callback(f"pet_skill_up:{pet_id}")
    bot = _bot()
    await pets.handle_pet_skill_up(callback, state, bot)

    assert callback.answer.await_args.args[0] == pets.SKILL_UP_NO_DIAMONDS_ALERT.format(
        cost=fa_digits(COST)
    )
    assert callback.answer.await_args.kwargs["show_alert"] is True
    bot.edit_message_media.assert_not_awaited()
    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.skill_level == 1


async def test_skill_up_callback_insufficient_diamonds_alert_text(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=500, is_adult=True, skill_level=1)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=COST - 1))

    callback = _callback(f"pet_skill_up:{pet_id}")
    await pets.handle_pet_skill_up(callback, state, _bot())

    # The exact alert the product asked for, with the cost in Persian digits.
    assert callback.answer.await_args.args[0] == (
        f"شما سیب الماسی کافی ندارید! (نیاز: {fa_digits(COST)})"
    )


async def test_skill_up_callback_alert_reflects_a_configured_cost(db, state):
    async with db.begin() as session:
        session.add(SystemConfig(key="pet_skill_upgrade_diamond_cost", value="25"))
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=500, is_adult=True, skill_level=1)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=24))

    callback = _callback(f"pet_skill_up:{pet_id}")
    await pets.handle_pet_skill_up(callback, state, _bot())

    assert callback.answer.await_args.args[0] == (
        f"شما سیب الماسی کافی ندارید! (نیاز: {fa_digits(25)})"
    )


async def test_skill_up_callback_success_updates_the_message(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=500, is_adult=True, skill_level=1,
                      last_fed_at=datetime.now(timezone.utc))
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=COST))

    callback = _callback(f"pet_skill_up:{pet_id}")
    bot = _bot()
    await pets.handle_pet_skill_up(callback, state, bot)

    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    caption = edited["media"].caption
    assert "لول ۲" in caption  # the new skill level is shown
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "cat_adult.jpg")
    # Still below the cap: the upgrade button remains for the next level.
    callbacks = [b.callback_data for b in _buttons(edited["reply_markup"])]
    assert f"pet_skill_up:{pet_id}" in callbacks

    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.skill_level == 2
        assert await ItemService.get_total(session, 1, DIAMOND) == 0


async def test_skill_up_callback_to_max_hides_the_button(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="owl", custom_name="پشمک",
                      xp=500, is_adult=True, skill_level=2,
                      last_fed_at=datetime.now(timezone.utc))
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=COST))

    callback = _callback(f"pet_skill_up:{pet_id}")
    bot = _bot()
    await pets.handle_pet_skill_up(callback, state, bot)

    edited = bot.edit_message_media.await_args.kwargs
    assert "لول ۳" in edited["media"].caption
    callbacks = [b.callback_data for b in _buttons(edited["reply_markup"])]
    assert f"pet_skill_up:{pet_id}" not in callbacks  # capped
    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.skill_level == 3


async def test_skill_up_callback_at_max_level(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=500, is_adult=True, skill_level=3)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=COST))

    callback = _callback(f"pet_skill_up:{pet_id}")
    bot = _bot()
    await pets.handle_pet_skill_up(callback, state, bot)

    assert callback.answer.await_args.args[0] == pets.SKILL_UP_MAX_LEVEL_ALERT
    bot.edit_message_media.assert_not_awaited()
    async with db.begin() as session:
        assert await ItemService.get_total(session, 1, DIAMOND) == COST


async def test_skill_up_callback_refuses_a_baby(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=0, is_adult=False, skill_level=1)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=COST))

    callback = _callback(f"pet_skill_up:{pet_id}")
    bot = _bot()
    await pets.handle_pet_skill_up(callback, state, bot)

    assert callback.answer.await_args.args[0] == pets.SKILL_UP_NOT_ADULT_ALERT
    bot.edit_message_media.assert_not_awaited()


async def test_skill_up_callback_refuses_another_players_pet(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=2, pet_type="dog", custom_name="rex",
                      xp=500, is_adult=True, skill_level=1)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key=DIAMOND, quantity=COST))

    callback = _callback(f"pet_skill_up:{pet_id}", clicker_id=1)
    bot = _bot()
    await pets.handle_pet_skill_up(callback, state, bot)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    bot.edit_message_media.assert_not_awaited()
    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.skill_level == 1
        assert await ItemService.get_total(session, 1, DIAMOND) == COST


async def test_skill_up_callback_is_registered():
    registered = [
        handler
        for handler in pets.router.callback_query.handlers
        if handler.callback is pets.handle_pet_skill_up
    ]
    assert registered
    event = SimpleNamespace(data="pet_skill_up:1")
    assert all([await flt.call(event, bot=None) for flt in registered[0].filters])


# ---------------------------------------------------------------------------
# Handlers — feeding an adult (satiety) and the disabled-skill profile
# ---------------------------------------------------------------------------

async def test_feed_callback_satiates_a_hungry_adult(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس",
                      xp=500, is_adult=True, skill_level=2,
                      last_fed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=1))

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    caption = edited["media"].caption
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "cat_adult.jpg")
    assert "سیر" in caption
    # The skill is active again: no 🔴 disabled marker.
    assert "🔴" not in caption
    assert f"{fa_digits(500)}/{fa_digits(500)}" in caption  # XP unchanged
    # Feeding stays available for the next hunger cycle.
    callbacks = [b.callback_data for b in _buttons(edited["reply_markup"])]
    assert f"pet_feed:{pet_id}" in callbacks

    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.xp == 500
        assert stored.last_fed_at is not None
        assert await ItemService.get_total(session, 1, "pet_food") == 0


async def test_pet_view_shows_disabled_skill_for_a_hungry_adult(db, state):
    async with db.begin() as session:
        session.add(UserPet(
            user_id=1, pet_type="cat", custom_name="ملوس",
            xp=500, is_adult=True, skill_level=2,
            last_fed_at=datetime.now(timezone.utc) - timedelta(hours=30),
        ))

    callback = _callback("pet_view:1")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    caption = bot.edit_message_media.await_args.kwargs["media"].caption
    assert pets.SATIETY_HUNGRY_LINE in caption
    assert "🔴" in caption


async def test_pet_view_shows_active_skill_for_a_full_adult(db, state):
    async with db.begin() as session:
        session.add(UserPet(
            user_id=1, pet_type="cat", custom_name="ملوس",
            xp=500, is_adult=True, skill_level=2,
            last_fed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))

    callback = _callback("pet_view:1")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    caption = bot.edit_message_media.await_args.kwargs["media"].caption
    assert pets.SATIETY_FULL_LINE in caption
    assert "🔴" not in caption
