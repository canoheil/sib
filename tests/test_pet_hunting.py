"""Pet System phase 2: hunting, cooldowns, caps and the naming flow.

Backend + handler level: the service owns every rule
(:mod:`sib.services.pet_service`) and :mod:`sib.handlers.pets` only renders the
result, so both are exercised here with isolated SQLite databases.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    PET_TYPES,
    Character,
    PlayerCard,
    SystemConfig,
    User,
    UserPet,
)
from sib.handlers import pets
from sib.handlers.pets import PetStates
from sib.services import pet_service

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _clean_pending_hunts():
    """The pet-less fallback cooldown map is process-global: keep tests apart."""
    pet_service._PENDING_HUNT_ATTEMPTS.clear()
    yield
    pet_service._PENDING_HUNT_ATTEMPTS.clear()


@pytest_asyncio.fixture
async def engine():
    """In-memory SQLite with only the tables this feature touches.

    SQLite autoincrements plain INTEGER primary keys only, so ``BigInteger``
    primary keys are narrowed for the DDL and restored immediately after (the
    same shim as ``tests/conftest.py``).
    """
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        User.__table__,
        SystemConfig.__table__,
        UserPet.__table__,
        Character.__table__,
        PlayerCard.__table__,
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
    async with factory() as db:
        yield db


@pytest_asyncio.fixture
async def db(engine, monkeypatch):
    """Session factory shared with the handler through a patched ``get_session``."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def get_session():
        async with factory.begin() as db_session:
            yield db_session

    monkeypatch.setattr(pets, "get_session", get_session)
    async with factory.begin() as db_session:
        db_session.add(User(id=1, first_name="pets player"))
        db_session.add(User(id=2, first_name="second player"))
    yield factory


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


class StubRng:
    """Deterministic stand-in for ``random.Random`` (roll + chosen species)."""

    def __init__(self, roll: float, pet_type: str = "cat"):
        self.roll = roll
        self.pet_type = pet_type
        self.choice_calls: list[tuple[str, ...]] = []

    def random(self) -> float:
        return self.roll

    def choice(self, sequence):
        self.choice_calls.append(tuple(sequence))
        assert self.pet_type in sequence
        return self.pet_type


async def _seed_pet(
    session,
    *,
    user_id: int = 1,
    pet_type: str = "dog",
    is_adult: bool = True,
    last_hunted_at: datetime | None = None,
    custom_name: str | None = None,
) -> UserPet:
    pet = UserPet(
        user_id=user_id,
        pet_type=pet_type,
        is_adult=is_adult,
        custom_name=custom_name,
        xp=0,
        skill_level=1,
        last_hunted_at=last_hunted_at,
    )
    session.add(pet)
    await session.flush()
    return pet


async def _pets(session, user_id: int = 1) -> list[UserPet]:
    return list(
        (
            await session.scalars(
                sa.select(UserPet)
                .where(UserPet.user_id == user_id)
                .order_by(UserPet.id)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Chance formula
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("card_power", "expected"),
    [
        (0, 30),
        (10, 32),
        (100, 50),
        (250, 80),
        (300, 90),
        (1000, 90),  # capped
        (-40, 30),  # clamped
    ],
)
def test_hunt_chance_formula(card_power, expected):
    assert pet_service.hunt_chance(card_power) == expected


def test_hunt_chance_matches_the_documented_formula():
    for power in (0, 7, 42, 123, 999):
        assert pet_service.hunt_chance(power) == int(min(90, 30 + power / 5))


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

async def test_cooldown_is_free_for_a_fresh_player(session):
    assert await pet_service.check_hunt_cooldown(session, 1) is False
    assert await pet_service.cooldown_remaining_seconds(session, 1) == 0
    assert await pet_service.cooldown_remaining_seconds(session, 1) == 0


async def test_cooldown_defaults_to_two_hours_after_hunting(session):
    await _seed_pet(session, pet_type="cat", is_adult=False, last_hunted_at=NOW)

    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is True
    remaining = await pet_service.cooldown_remaining_seconds(
        session, 1, now=NOW + timedelta(minutes=30)
    )
    assert remaining == 90 * 60
    # Two hours exactly: ready again.
    assert (
        await pet_service.check_hunt_cooldown(session, 1, now=NOW + timedelta(hours=2))
        is False
    )


async def test_cooldown_honours_the_system_config_override(session):
    session.add(SystemConfig(key="pet_hunt_cooldown_hours", value="5"))
    await _seed_pet(session, is_adult=True, last_hunted_at=NOW)
    await session.flush()

    assert (
        await pet_service.check_hunt_cooldown(session, 1, now=NOW + timedelta(hours=3))
        is True
    )
    assert (
        await pet_service.check_hunt_cooldown(session, 1, now=NOW + timedelta(hours=5))
        is False
    )


async def test_zero_cooldown_config_disables_the_wait(session):
    session.add(SystemConfig(key="pet_hunt_cooldown_hours", value="0"))
    await _seed_pet(session, is_adult=True, last_hunted_at=NOW)
    await session.flush()

    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is False


# ---------------------------------------------------------------------------
# Hunting
# ---------------------------------------------------------------------------

async def test_successful_hunt_creates_a_nameless_baby(session):
    rng = StubRng(roll=0.0, pet_type="squirrel")

    result = await pet_service.hunt_pet(
        session, 1, card_power=100, rng=rng, now=NOW
    )

    assert result["status"] == "success"
    assert result["chance"] == 50
    assert result["pet_type"] == "squirrel"
    assert result["pet"].pet_type == "squirrel"
    assert rng.choice_calls == [tuple(PET_TYPES)]

    pets = await _pets(session)
    assert len(pets) == 1
    pet = pets[0]
    assert pet.id is not None
    assert pet.user_id == 1
    assert pet.pet_type == "squirrel"
    assert pet.custom_name is None  # named by the follow-up flow
    assert pet.is_adult is False
    assert pet.xp == 0
    assert pet.skill_level == 1
    assert pet.last_hunted_at.replace(tzinfo=timezone.utc) == NOW


async def test_failed_hunt_creates_no_pet_and_still_starts_the_cooldown(session):
    await _seed_pet(session, pet_type="owl", is_adult=True)
    rng = StubRng(roll=0.99)

    result = await pet_service.hunt_pet(session, 1, card_power=50, rng=rng, now=NOW)

    assert result == {"status": "failed", "chance": 40}
    assert rng.choice_calls == []  # no species is picked on a miss
    pets = await _pets(session)
    assert len(pets) == 1  # the failed hunt never creates a pet
    assert pets[0].last_hunted_at.replace(tzinfo=timezone.utc) == NOW
    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is True


async def test_failed_hunt_without_pets_falls_back_to_the_memory_cooldown(session):
    result = await pet_service.hunt_pet(
        session, 1, card_power=0, rng=StubRng(roll=0.99), now=NOW
    )

    assert result["status"] == "failed"
    assert await _pets(session) == []
    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is True
    assert (
        await pet_service.check_hunt_cooldown(session, 1, now=NOW + timedelta(hours=3))
        is False
    )


async def test_hunt_is_refused_while_cooling_down(session):
    await pet_service.hunt_pet(session, 1, 100, rng=StubRng(0.0, "cat"), now=NOW)

    rng = StubRng(0.0, "dog")
    result = await pet_service.hunt_pet(
        session, 1, 100, rng=rng, now=NOW + timedelta(minutes=10)
    )

    assert result["status"] == "cooldown"
    assert result["remaining_seconds"] == 110 * 60
    assert rng.choice_calls == []
    assert len(await _pets(session)) == 1  # nothing new was created


async def test_only_one_baby_at_a_time(session):
    await _seed_pet(session, pet_type="cat", is_adult=False)

    result = await pet_service.hunt_pet(
        session, 1, 100, rng=StubRng(0.0), now=NOW
    )

    assert result["status"] == "limit"
    assert result["reason"] == "baby"
    assert result["counts"] == {"total": 1, "babies": 1}
    assert len(await _pets(session)) == 1


async def test_total_pet_cap_is_three(session):
    for pet_type in PET_TYPES[: pet_service.MAX_PETS_TOTAL]:
        await _seed_pet(session, pet_type=pet_type, is_adult=True)

    result = await pet_service.hunt_pet(session, 1, 100, rng=StubRng(0.0), now=NOW)

    assert result["status"] == "limit"
    assert result["reason"] == "total"
    assert result["max_total"] == 3
    assert len(await _pets(session)) == 3


async def test_pet_counts_are_per_player(session):
    await _seed_pet(session, user_id=1, is_adult=True)
    await _seed_pet(session, user_id=1, is_adult=False)
    await _seed_pet(session, user_id=2, is_adult=True)

    assert await pet_service.pet_counts(session, 1) == {"total": 2, "babies": 1}
    assert await pet_service.pet_counts(session, 2) == {"total": 1, "babies": 0}
    assert await pet_service.pet_counts(session, 3) == {"total": 0, "babies": 0}


# ---------------------------------------------------------------------------
# Naming rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("سیبک", "سیبک"),
        ("  سیبک  ", "سیبک"),  # trimmed
        ("پت   من", "پت من"),  # inner runs collapsed
        ("سیب\u200cک", "سیب\u200cک"),  # ZWNJ inside a Persian word
        ("سیبک ۲", "سیبک ۲"),  # digits next to letters are fine
        ("ab", "ab"),
        ("x" * 20, "x" * 20),
    ],
)
def test_validate_pet_name_accepts_reasonable_names(raw, expected):
    assert pet_service.validate_pet_name(raw) == (True, expected)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        (None, "empty"),
        ("a", "too_short"),
        ("x" * 21, "too_long"),
        ("12345", "digits_only"),
        ("۱۲۳", "digits_only"),
        ("بد!", "invalid_chars"),
        ("<script>", "invalid_chars"),
        ("🐺", "invalid_chars"),
    ],
)
def test_validate_pet_name_rejects_bad_names(raw, reason):
    assert pet_service.validate_pet_name(raw) == (False, reason)


async def test_name_pet_stores_the_custom_name(session):
    pet = await _seed_pet(session, is_adult=False)

    result = await pet_service.name_pet(session, 1, pet.id, "  سیبک  ")

    assert result["status"] == "named"
    assert result["name"] == "سیبک"
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.custom_name == "سیبک"


async def test_name_pet_refuses_another_players_pet(session):
    pet = await _seed_pet(session, user_id=2, is_adult=False)

    result = await pet_service.name_pet(session, 1, pet.id, "دزدی")

    assert result == {"status": "not_found"}
    stored = await session.get(UserPet, pet.id)
    assert stored.custom_name is None


async def test_name_pet_reports_the_validation_reason(session):
    pet = await _seed_pet(session, is_adult=False)

    assert await pet_service.name_pet(session, 1, pet.id, "1") == {
        "status": "digits_only"
    }
    assert await pet_service.name_pet(session, 1, pet.id, "a") == {
        "status": "too_short"
    }
    stored = await session.get(UserPet, pet.id)
    assert stored.custom_name is None


# ---------------------------------------------------------------------------
# Handlers — hunting
# ---------------------------------------------------------------------------

async def test_hunt_aliases_and_command_are_registered():
    from sib.handlers import pets as pets_module

    assert "سیب شکار" in pets_module.HUNT_ALIASES
    assert "شکار سیبی" in pets_module.HUNT_ALIASES

    registered = [
        handler
        for handler in pets_module.router.message.handlers
        if handler.callback is pets_module.handle_hunt
    ]
    assert registered

    for text in ("/hunt", "سیب شکار", "شکار سیبی", "سیب شکار"):
        event = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=TelegramUser(id=1, first_name="player", is_bot=False),
            text=text,
        )
        matched = False
        for entry in registered:
            if all([await flt.call(event, bot=None) for flt in entry.filters]):
                matched = True
                break
        assert matched, f"no filter matched {text!r}"


def test_pets_router_is_registered_in_the_dispatcher():
    from sib.handlers import register_all_handlers

    included = []
    with patch.object(Dispatcher, "include_router", side_effect=included.append):
        register_all_handlers(Dispatcher())
    assert pets.router in included


async def test_hunt_handler_sends_the_baby_artwork_and_waits_for_a_name(
    db, state, monkeypatch
):
    async def fake_hunt(session, user_id, card_power, **_kwargs):
        return {
            "status": "success",
            "pet": SimpleNamespace(id=7, pet_type="owl"),
            "pet_type": "owl",
            "chance": 50,
        }

    monkeypatch.setattr(pets.pet_service, "hunt_pet", fake_hunt)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    message.reply_photo.assert_awaited_once()
    sent = message.reply_photo.await_args
    assert sent.kwargs["caption"] == pets.SUCCESS_CAPTION.format(type_fa="جغد")
    assert sent.kwargs["parse_mode"] == "HTML"
    assert str(pets.ASSET_DIR / "owl_baby.jpg") == str(sent.args[0].path)

    assert await state.get_state() == PetStates.waiting_for_name.state
    assert (await state.get_data())["pet_id"] == 7


@pytest.mark.parametrize(
    ("result", "expected_fragment"),
    [
        ({"status": "failed", "chance": 45}, "تلاش برای شکار ناموفق بود"),
        ({"status": "cooldown", "remaining_seconds": 7200}, "۲ ساعت"),
        ({"status": "limit", "reason": "baby"}, "بچه پت"),
        (
            {"status": "limit", "reason": "total", "max_total": 3},
            "ظرفیت شما پر است",
        ),
    ],
)
async def test_hunt_handler_renders_every_refusal(
    db, state, monkeypatch, result, expected_fragment
):
    async def fake_hunt(session, user_id, card_power, **_kwargs):
        return result

    monkeypatch.setattr(pets.pet_service, "hunt_pet", fake_hunt)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    message.reply.assert_awaited_once()
    assert expected_fragment in message.reply.await_args.args[0]
    message.reply_photo.assert_not_awaited()
    assert await state.get_state() is None

    if result["status"] == "failed":
        assert "۴۵" in message.reply.await_args.args[0]


async def test_hunt_handler_uses_the_main_card_power(db, state, monkeypatch):
    captured = {}

    async def fake_hunt(session, user_id, card_power, **_kwargs):
        captured["power"] = card_power
        return {"status": "failed", "chance": pet_service.hunt_chance(card_power)}

    monkeypatch.setattr(pets.pet_service, "hunt_pet", fake_hunt)

    async with db.begin() as session:
        session.add(
            Character(id=5, code="hero", name_fa="قهرمان", power=40, speed=10, intelligence=10)
        )
        await session.flush()
        session.add(
            PlayerCard(user_id=1, character_id=5, level=1, is_favorite=True)
        )

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    assert captured["power"] == 40  # Character.power at card level 1
    assert "۳۸" in message.reply.await_args.args[0]  # 30 + 40/5


async def test_main_card_power_falls_back_to_unarmed(db):
    async with db.begin() as session:
        assert await pets.main_card_power(session, 1) == pets.BASE_UNARMED_POWER


# ---------------------------------------------------------------------------
# Handlers — naming
# ---------------------------------------------------------------------------

async def _seed_naming_state(db, state, text: str):
    """An unnamed pet for player 1, with the naming state waiting on it."""
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", is_adult=False, xp=0, skill_level=1)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
    await state.set_state(PetStates.waiting_for_name)
    await state.update_data(pet_id=pet_id)
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        text=text,
        reply=AsyncMock(),
    )


async def test_naming_handler_saves_the_name_and_clears_the_state(db, state):
    pet_id = None
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", is_adult=False)
        session.add(pet)
        await session.flush()
        pet_id = pet.id

    await state.set_state(PetStates.waiting_for_name)
    await state.update_data(pet_id=pet_id)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        text="ملوس",
        reply=AsyncMock(),
    )
    await pets.handle_pet_name(message, state)

    message.reply.assert_awaited_once()
    assert "ملوس" in message.reply.await_args.args[0]
    assert "سیب پت" in message.reply.await_args.args[0]
    assert await state.get_state() is None
    assert await state.get_data() == {}

    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.custom_name == "ملوس"


async def test_naming_handler_rejects_invalid_names_and_keeps_the_state(db, state):
    message = await _seed_naming_state(db, state, "۱۲۳")

    await pets.handle_pet_name(message, state)

    message.reply.assert_awaited_once()
    assert message.reply.await_args.args[0] == pets.NAME_ERRORS["digits_only"]
    # The state survives so the player can simply retype a name.
    assert await state.get_state() == PetStates.waiting_for_name.state

    async with db.begin() as session:
        stored = await session.scalar(sa.select(UserPet).where(UserPet.user_id == 1))
        assert stored.custom_name is None


async def test_naming_handler_without_a_pet_id_asks_to_hunt(db, state):
    await state.set_state(PetStates.waiting_for_name)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        text="ملوس",
        reply=AsyncMock(),
    )

    await pets.handle_pet_name(message, state)

    message.reply.assert_awaited_once_with(pets.NO_PET_TEXT)
    assert await state.get_state() is None


async def test_naming_handler_escapes_html_in_the_confirmation(db, state, monkeypatch):
    """Defensive: validation rejects markup, but the reply is escaped anyway."""
    async def fake_name_pet(session, user_id, pet_id, raw):
        return {"status": "named", "name": "<b>سیبک & co</b>"}

    monkeypatch.setattr(pets.pet_service, "name_pet", fake_name_pet)

    await state.set_state(PetStates.waiting_for_name)
    await state.update_data(pet_id=1)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        text="whatever",
        reply=AsyncMock(),
    )

    await pets.handle_pet_name(message, state)

    text = message.reply.await_args.args[0]
    assert "&lt;b&gt;سیبک &amp; co&lt;/b&gt;" in text
    assert "<b>" not in text


async def test_naming_handler_rejects_markup_and_emoji(db, state):
    for raw in ("<b>x</b>", "پت 🐺"):
        message = await _seed_naming_state(db, state, raw)

        await pets.handle_pet_name(message, state)

        assert message.reply.await_args.args[0] == pets.NAME_ERRORS["invalid_chars"]
        assert await state.get_state() == PetStates.waiting_for_name.state


def test_format_remaining_is_persian():
    assert pets.format_remaining(0) == "۱ ثانیه"
    assert pets.format_remaining(90) == "۱ دقیقه"
    assert pets.format_remaining(3600) == "۱ ساعت"
    assert pets.format_remaining(9000) == "۲ ساعت و ۳۰ دقیقه"


async def test_hunt_pet_uses_a_real_rng_when_none_is_injected(session, monkeypatch):
    stub = StubRng(roll=0.0, pet_type="dog")
    monkeypatch.setattr(pet_service.random, "Random", lambda: stub)

    result = await pet_service.hunt_pet(session, 1, card_power=100, now=NOW)

    assert result["status"] == "success"
    assert result["pet_type"] == "dog"
    assert stub.choice_calls == [tuple(PET_TYPES)]



async def test_naming_handler_only_listens_to_plain_text(db, state):
    """A command typed while naming must not be stored as a pet name."""
    registered = [
        handler
        for handler in pets.router.message.handlers
        if handler.callback is pets.handle_pet_name
    ]
    assert registered

    async def matches(text: str, chat_type: str = "private") -> bool:
        event = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type=chat_type),
            from_user=TelegramUser(id=1, first_name="player", is_bot=False),
            text=text,
        )
        return all(
            [
                # ``raw_state`` is what the dispatcher's FSM middleware passes
                # to StateFilter.
                await flt.call(
                    event,
                    bot=None,
                    raw_state=PetStates.waiting_for_name.state,
                )
                for flt in registered[0].filters
            ]
        )

    assert await matches("ملوس") is True
    assert await matches("/hunt") is False
    assert await matches("/start") is False
