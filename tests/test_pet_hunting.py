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
# Encounters — step 1 (get_encounter)
# ---------------------------------------------------------------------------

async def test_get_encounter_rolls_the_species_but_not_the_success(session):
    rng = StubRng(roll=0.0, pet_type="squirrel")

    result = await pet_service.get_encounter(
        session, 1, card_power=100, rng=rng, now=NOW
    )

    assert result == {"status": "ok", "pet_type": "squirrel", "chance": 50}
    assert rng.choice_calls == [tuple(PET_TYPES)]
    # Opening an encounter creates nothing and never starts the cooldown.
    assert await _pets(session) == []
    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is False


async def test_get_encounter_defaults_to_the_base_chance(session):
    result = await pet_service.get_encounter(session, 1, rng=StubRng(0.0, "cat"))

    assert result["chance"] == pet_service.BASE_HUNT_CHANCE == 30


async def test_get_encounter_is_refused_while_cooling_down(session):
    await _seed_pet(session, pet_type="owl", is_adult=True, last_hunted_at=NOW)
    rng = StubRng(roll=0.0, pet_type="dog")

    result = await pet_service.get_encounter(
        session, 1, card_power=100, rng=rng, now=NOW + timedelta(minutes=10)
    )

    assert result["status"] == "cooldown"
    assert result["remaining_seconds"] == 110 * 60
    assert rng.choice_calls == []  # nothing is rolled while blocked


async def test_get_encounter_refuses_a_second_baby(session):
    await _seed_pet(session, pet_type="cat", is_adult=False)

    result = await pet_service.get_encounter(session, 1, rng=StubRng(0.0, "dog"), now=NOW)

    assert result["status"] == "limit"
    assert result["reason"] == "baby"
    assert result["counts"] == {"total": 1, "babies": 1}


async def test_get_encounter_refuses_past_the_total_cap(session):
    for pet_type in PET_TYPES[: pet_service.MAX_PETS_TOTAL]:
        await _seed_pet(session, pet_type=pet_type, is_adult=True)

    result = await pet_service.get_encounter(session, 1, rng=StubRng(0.0), now=NOW)

    assert result["status"] == "limit"
    assert result["reason"] == "total"
    assert result["max_total"] == 3


# ---------------------------------------------------------------------------
# Encounters — step 2 (execute_hunt)
# ---------------------------------------------------------------------------

async def test_execute_hunt_success_creates_a_nameless_baby(session):
    rng = StubRng(roll=0.0)

    result = await pet_service.execute_hunt(
        session, 1, "squirrel", 50, rng=rng, now=NOW
    )

    assert result["status"] == "success"
    assert result["pet_type"] == "squirrel"
    assert result["pet"].pet_type == "squirrel"

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


async def test_execute_hunt_failure_creates_no_pet_and_starts_the_cooldown(session):
    await _seed_pet(session, pet_type="owl", is_adult=True)

    result = await pet_service.execute_hunt(
        session, 1, "owl", 40, rng=StubRng(0.99), now=NOW
    )

    assert result == {
        "status": "failed",
        "pet": None,
        "pet_type": "owl",
        "chance": 40,
    }
    pets = await _pets(session)
    assert len(pets) == 1  # the miss never creates a pet
    assert pets[0].last_hunted_at.replace(tzinfo=timezone.utc) == NOW
    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is True


async def test_execute_hunt_failure_without_pets_uses_the_memory_cooldown(session):
    result = await pet_service.execute_hunt(
        session, 1, "dog", 30, rng=StubRng(0.99), now=NOW
    )

    assert result["status"] == "failed"
    assert await _pets(session) == []
    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is True
    assert (
        await pet_service.check_hunt_cooldown(session, 1, now=NOW + timedelta(hours=3))
        is False
    )


async def test_execute_hunt_rolls_against_the_given_chance(session):
    """A roll just under the chance succeeds; just over it fails."""
    at_edge = await pet_service.execute_hunt(
        session, 1, "cat", 50, rng=StubRng(0.4999), now=NOW
    )
    assert at_edge["status"] == "success"


async def test_execute_hunt_rejects_a_forged_species(session):
    result = await pet_service.execute_hunt(
        session, 1, "dragon", 90, rng=StubRng(0.0), now=NOW
    )

    assert result == {"status": "invalid", "pet": None}
    assert await _pets(session) == []
    assert await pet_service.check_hunt_cooldown(session, 1, now=NOW) is False


@pytest.mark.parametrize("forged", [500, 100, 91, 99])
async def test_execute_hunt_clamps_a_forged_chance(session, forged):
    """Callback data is client-editable: the chance is capped at 90 %."""
    result = await pet_service.execute_hunt(
        session, 1, "cat", forged, rng=StubRng(0.95), now=NOW
    )

    assert result["chance"] == pet_service.MAX_HUNT_CHANCE == 90
    assert result["status"] == "failed"  # roll 95 > 90


async def test_execute_hunt_double_press_cannot_create_two_pets(session):
    first = await pet_service.execute_hunt(
        session, 1, "cat", 90, rng=StubRng(0.0), now=NOW
    )
    second = await pet_service.execute_hunt(
        session, 1, "dog", 90, rng=StubRng(0.0), now=NOW
    )

    assert first["status"] == "success"
    assert second["status"] == "cooldown"  # the button cannot be replayed
    assert len(await _pets(session)) == 1


async def test_execute_hunt_still_enforces_the_pet_caps(session):
    await _seed_pet(session, pet_type="cat", is_adult=False)

    result = await pet_service.execute_hunt(
        session, 1, "dog", 90, rng=StubRng(0.0), now=NOW
    )

    assert result["status"] == "limit"
    assert result["pet"] is None
    assert len(await _pets(session)) == 1


async def test_pet_counts_are_per_player(session):
    await _seed_pet(session, user_id=1, is_adult=True)
    await _seed_pet(session, user_id=1, is_adult=False)
    await _seed_pet(session, user_id=2, is_adult=True)

    assert await pet_service.pet_counts(session, 1) == {"total": 2, "babies": 1}
    assert await pet_service.pet_counts(session, 2) == {"total": 1, "babies": 0}
    assert await pet_service.pet_counts(session, 3) == {"total": 0, "babies": 0}


# ---------------------------------------------------------------------------
# Tracking mini-game — the hidden path and the adjusted chance
# ---------------------------------------------------------------------------

def test_track_step_choices_cover_every_step():
    assert set(pet_service.TRACK_STEP_CHOICES) == {1, 2, 3}
    for step, choices in pet_service.TRACK_STEP_CHOICES.items():
        assert len(choices) == 2


class _FirstChoiceRng:
    """A ``random.Random`` stand-in that always picks the first choice."""

    def random(self) -> float:
        return 0.0

    def choice(self, sequence):
        return sequence[0]


def test_roll_tracking_path_picks_one_choice_per_step():
    path = pet_service.roll_tracking_path(rng=_FirstChoiceRng())

    assert len(path) == 3
    for step, choice in enumerate(path, start=1):
        assert choice in pet_service.TRACK_STEP_CHOICES[step]
    assert path == ["left", "cave", "now"]


def test_roll_tracking_path_uses_a_real_rng_when_none_is_injected(monkeypatch):
    monkeypatch.setattr(pet_service.random, "Random", lambda: _FirstChoiceRng())

    path = pet_service.roll_tracking_path()

    assert path == ["left", "cave", "now"]


def test_correct_choice_compares_against_the_rolled_path():
    path = ["right", "cave", "now"]

    assert pet_service.correct_choice(1, "right", path) is True
    assert pet_service.correct_choice(1, "left", path) is False
    assert pet_service.correct_choice(2, "cave", path) is True
    assert pet_service.correct_choice(2, "river", path) is False
    assert pet_service.correct_choice(3, "now", path) is True
    assert pet_service.correct_choice(3, "wait", path) is False
    # Out-of-range steps and truncated paths never count as correct.
    assert pet_service.correct_choice(4, "now", path) is False
    assert pet_service.correct_choice(0, "right", path) is False
    assert pet_service.correct_choice(1, "right", ["left"]) is False


def test_tracking_adjusted_chance_adds_and_subtracts():
    path = ["right", "cave", "now"]

    assert pet_service.tracking_adjusted_chance(50, [], path) == 50
    assert pet_service.tracking_adjusted_chance(50, ["right"], path) == 60
    assert pet_service.tracking_adjusted_chance(50, ["left"], path) == 40
    # Three correct: +30; three wrong: -30.
    assert pet_service.tracking_adjusted_chance(50, ["right", "cave", "now"], path) == 80
    assert pet_service.tracking_adjusted_chance(50, ["left", "river", "wait"], path) == 20
    # A mixed path: right ✓, river ✗, wait ✗ → 50 + 10 - 10 - 10.
    assert pet_service.tracking_adjusted_chance(50, ["right", "river", "wait"], path) == 40


def test_tracking_adjusted_chance_is_clamped():
    path = ["right", "cave", "now"]

    assert pet_service.tracking_adjusted_chance(90, ["right", "cave", "now"], path) == 90
    assert pet_service.tracking_adjusted_chance(10, ["left", "river", "wait"], path) == 0
    # Choices beyond the rolled path length are treated as wrong.
    assert pet_service.tracking_adjusted_chance(50, ["right", "cave", "now", "now"], path) == 70
    # A nonsensical base falls back to the base chance.
    assert pet_service.tracking_adjusted_chance(None, [], path) == pet_service.BASE_HUNT_CHANCE


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


async def test_hunt_handler_opens_the_encounter_and_starts_tracking(
    db, state, monkeypatch
):
    async def fake_encounter(session, user_id, card_power, **_kwargs):
        return {"status": "ok", "pet_type": "owl", "chance": pet_service.hunt_chance(card_power)}

    monkeypatch.setattr(pets.pet_service, "get_encounter", fake_encounter)
    monkeypatch.setattr(
        pets.pet_service, "roll_tracking_path", lambda **_kwargs: ["right", "cave", "now"]
    )

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    message.reply_photo.assert_awaited_once()
    sent = message.reply_photo.await_args
    # The tracking game opens on the forest plate, not the adult artwork.
    assert str(sent.args[0].path) == str(pets.FOREST_IMAGE)
    assert sent.kwargs["parse_mode"] == "HTML"

    # The caption shows the species, the card power, the chance and step 1.
    caption = sent.kwargs["caption"]
    assert "وارد جنگل جادویی شدی" in caption
    assert "<b>جغد وحشی</b>" in caption  # **bold** rendered as HTML
    assert "۱۰" in caption  # BASE_UNARMED_POWER
    assert "۳۲" in caption  # 30 + 10/5
    assert pets.STEP1_CAPTION in caption

    # The two step-1 buttons carry the documented callback payloads.
    keyboard = sent.kwargs["reply_markup"]
    buttons = keyboard.inline_keyboard[0]
    assert [b.text for b in buttons] == ["⬅️ سمت چپ", "➡️ سمت راست"]
    assert [b.callback_data for b in buttons] == [
        "hunt_track:step1:left",
        "hunt_track:step1:right",
    ]

    # The encounter starts the tracking FSM with the rolled hidden path.
    assert await state.get_state() == PetStates.tracking.state
    data = await state.get_data()
    assert data["pet_type"] == "owl"
    assert data["base_chance"] == 32
    assert data["path"] == ["right", "cave", "now"]
    assert data["choices"] == []
    message.reply.assert_not_awaited()


async def test_hunt_handler_rolls_a_fresh_path_and_abandons_a_tracking_state(
    db, state, monkeypatch
):
    """A second «سیب شکار» resets the game: new path, no stale choices."""
    monkeypatch.setattr(
        pets.pet_service, "roll_tracking_path", lambda **_kwargs: ["left", "river", "wait"]
    )

    async def fake_encounter(session, user_id, card_power, **_kwargs):
        return {"status": "ok", "pet_type": "cat", "chance": 40}

    monkeypatch.setattr(pets.pet_service, "get_encounter", fake_encounter)

    # A half-finished tracking game from a previous encounter.
    await state.set_state(PetStates.tracking)
    await state.update_data(
        pet_type="owl", base_chance=30, path=["right"], choices=["right"]
    )

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    data = await state.get_data()
    assert data["path"] == ["left", "river", "wait"]
    assert data["choices"] == []
    assert data["pet_type"] == "cat"
    assert data["base_chance"] == 40


@pytest.mark.parametrize(
    ("result", "expected_fragment"),
    [
        ({"status": "cooldown", "remaining_seconds": 7200}, "۲ ساعت"),
        ({"status": "limit", "reason": "baby"}, "بچه پت"),
        (
            {"status": "limit", "reason": "total", "max_total": 3},
            "ظرفیت شما پر است",
        ),
    ],
)
async def test_hunt_handler_renders_every_encounter_refusal(
    db, state, monkeypatch, result, expected_fragment
):
    async def fake_encounter(session, user_id, card_power, **_kwargs):
        return result

    monkeypatch.setattr(pets.pet_service, "get_encounter", fake_encounter)

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


async def test_hunt_handler_uses_the_main_card_power(db, state, monkeypatch):
    captured = {}

    async def fake_encounter(session, user_id, card_power, **_kwargs):
        captured["power"] = card_power
        return {
            "status": "ok",
            "pet_type": "cat",
            "chance": pet_service.hunt_chance(card_power),
        }

    monkeypatch.setattr(pets.pet_service, "get_encounter", fake_encounter)

    async with db.begin() as session:
        session.add(
            Character(id=5, code="hero", name_fa="قهرمان", power=40, speed=10, intelligence=10)
        )
        await session.flush()
        session.add(PlayerCard(user_id=1, character_id=5, level=1, is_favorite=True))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    assert captured["power"] == 40  # Character.power at card level 1
    caption = message.reply_photo.await_args.kwargs["caption"]
    assert "۳۸" in caption  # 30 + 40/5
    assert "۴۰" in caption  # the power line


async def test_main_card_power_falls_back_to_unarmed(db):
    async with db.begin() as session:
        assert await pets.main_card_power(session, 1) == pets.BASE_UNARMED_POWER


# ---------------------------------------------------------------------------
# Handlers — the tracking mini-game
# ---------------------------------------------------------------------------

def _callback(
    data: str,
    *,
    clicker_id: int = 1,
    message_id: int = 99,
    chat_id: int = 1,
):
    """A callback query whose message carries an editable photo."""
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=clicker_id, is_bot=False),
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id),
            photo=[SimpleNamespace(file_id="f")],
            answer=AsyncMock(),
        ),
    )


def _bot():
    return SimpleNamespace(
        edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )


async def _start_tracking(state, path=None, base_chance=50, pet_type="owl"):
    """Put the FSM into the tracking state the encounter handler would."""
    await state.set_state(PetStates.tracking)
    await state.update_data(
        pet_type=pet_type,
        base_chance=base_chance,
        path=path or ["right", "cave", "now"],
        choices=[],
    )


async def test_track_step1_advances_to_step2(db, state, monkeypatch):
    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        raise AssertionError("the hunt must not roll before step 3")

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state)

    callback = _callback("hunt_track:step1:right")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    callback.answer.assert_awaited_once()  # acknowledged, no alert
    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert edited["chat_id"] == 1
    assert edited["message_id"] == 99
    assert str(edited["media"].media.path) == str(pets.FOREST_IMAGE)
    assert pets.STEP2_CAPTION in edited["media"].caption
    buttons = edited["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["🕳️ غار تاریک", "🌊 رودخانه"]
    assert [b.callback_data for b in buttons] == [
        "hunt_track:step2:cave",
        "hunt_track:step2:river",
    ]

    data = await state.get_data()
    assert data["choices"] == ["right"]


async def test_track_step2_advances_to_step3(db, state, monkeypatch):
    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        raise AssertionError("the hunt must not roll before step 3")

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state)
    await state.update_data(choices=["right"])

    callback = _callback("hunt_track:step2:cave")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert pets.STEP3_CAPTION in edited["media"].caption
    buttons = edited["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["🥅 همین حالا!", "⏳ کمی صبر می‌کنم"]
    assert [b.callback_data for b in buttons] == [
        "hunt_track:step3:now",
        "hunt_track:step3:wait",
    ]

    data = await state.get_data()
    assert data["choices"] == ["right", "cave"]


async def test_track_finale_correct_path_raises_the_chance_and_catches(
    db, state, monkeypatch
):
    captured = {}

    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        captured["chance"] = chance
        return {
            "status": "success",
            "pet": SimpleNamespace(id=77, pet_type=pet_type),
            "pet_type": pet_type,
            "chance": chance,
        }

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state, path=["right", "cave", "now"], base_chance=50)
    await state.update_data(choices=["right", "cave"])

    callback = _callback("hunt_track:step3:now")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    # 50 + 3 correct choices x 10 %.
    assert captured["chance"] == 80
    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "owl_baby.jpg")
    assert "شکار موفق" in edited["media"].caption
    assert edited["reply_markup"] is None  # the buttons disappear

    # A catch starts the naming flow.
    assert await state.get_state() == PetStates.waiting_for_name.state
    assert (await state.get_data())["pet_id"] == 77


async def test_track_finale_wrong_path_lowers_the_chance_and_fails(
    db, state, monkeypatch
):
    captured = {}

    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        captured["chance"] = chance
        return {"status": "failed", "pet": None, "pet_type": pet_type, "chance": chance}

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state, path=["right", "cave", "now"], base_chance=50)
    await state.update_data(choices=["left", "river"])

    callback = _callback("hunt_track:step3:wait")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    # 50 - 3 wrong choices x 10 %.
    assert captured["chance"] == 20
    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.FAIL_IMAGE)
    assert "فرار کرد" in edited["media"].caption

    # A miss never starts the naming flow.
    assert await state.get_state() is None


async def test_track_finale_partial_path_mixes_bonus_and_penalty(db, state, monkeypatch):
    captured = {}

    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        captured["chance"] = chance
        return {"status": "failed", "pet": None, "pet_type": pet_type, "chance": chance}

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state, path=["right", "cave", "now"], base_chance=40)
    await state.update_data(choices=["right", "river"])

    callback = _callback("hunt_track:step3:wait")
    await pets.handle_hunt_track(callback, state, _bot())

    # right ✓ +10, river ✗ -10, wait ✗ -10 → 40 - 10 = 30.
    assert captured["chance"] == 30


async def test_track_finale_chance_is_clamped(db, state, monkeypatch):
    captured = {}

    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        captured["chance"] = chance
        return {"status": "failed", "pet": None, "pet_type": pet_type, "chance": chance}

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    # A base chance of 90 with three correct choices must not exceed the cap.
    await _start_tracking(state, path=["right", "cave", "now"], base_chance=90)
    await state.update_data(choices=["right", "cave"])

    callback = _callback("hunt_track:step3:now")
    await pets.handle_hunt_track(callback, state, _bot())

    assert captured["chance"] == pet_service.MAX_HUNT_CHANCE == 90


async def test_track_step_out_of_order_is_refused(db, state, monkeypatch):
    """Pressing step 2 while step 1 is open must not skip ahead."""

    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        raise AssertionError("the hunt must not roll on an out-of-order click")

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state)

    callback = _callback("hunt_track:step2:cave")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert await state.get_state() == PetStates.tracking.state
    assert (await state.get_data())["choices"] == []


@pytest.mark.parametrize(
    "data",
    [
        "hunt_track:",
        "hunt_track:1",
        "hunt_track:step1:left:extra",
        "hunt_track:step0:left",
        "hunt_track:step4:now",
        "hunt_track:step1:up",
        "hunt_track:step2:fly",
        "hunt_track:abc:left",
    ],
)
async def test_track_callback_rejects_malformed_data(db, state, data):
    bot = _bot()
    callback = _callback(data)
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_track_callback_without_a_tracking_state_is_expired(db, state):
    """A stale button press after the game ended shows the gone alert."""
    bot = _bot()
    callback = _callback("hunt_track:step1:left")
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "منقضی" in callback.answer.await_args.args[0]


async def test_track_callback_after_the_naming_step_is_expired(db, state):
    """The naming state is a different game: tracking buttons are stale."""
    await state.set_state(PetStates.waiting_for_name)
    await state.update_data(pet_id=5)

    callback = _callback("hunt_track:step1:left")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    # The naming flow is untouched.
    assert await state.get_state() == PetStates.waiting_for_name.state


async def test_track_callback_corrupt_state_is_cleared(db, state):
    """A tracking state without its data cannot roll anything."""
    await state.set_state(PetStates.tracking)
    await state.update_data(pet_type="owl", base_chance=50, path=["right"])

    callback = _callback("hunt_track:step1:right")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert await state.get_state() is None


async def test_track_finale_reports_a_cooldown(db, state, monkeypatch):
    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        return {"status": "cooldown", "remaining_seconds": 7200, "pet": None}

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state)
    await state.update_data(choices=["right", "cave"])

    callback = _callback("hunt_track:step3:now")
    bot = _bot()
    await pets.handle_hunt_track(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "۲ ساعت" in callback.answer.await_args.args[0]


async def test_track_step_edit_falls_back_to_a_reply_when_the_edit_fails(
    db, state, monkeypatch
):
    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        raise AssertionError("the hunt must not roll before step 3")

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state)

    bot = SimpleNamespace(edit_message_media=AsyncMock(side_effect=RuntimeError("gone")))
    callback = _callback("hunt_track:step1:right")
    await pets.handle_hunt_track(callback, state, bot)

    # The player still sees step 2, even if the message cannot be edited.
    callback.message.answer.assert_awaited_once()
    assert pets.STEP2_CAPTION in callback.message.answer.await_args.args[0]


async def test_track_finale_falls_back_to_text_when_the_edit_fails(
    db, state, monkeypatch
):
    async def fake_execute(session, user_id, pet_type, chance, **_kwargs):
        return {"status": "failed", "pet": None, "pet_type": pet_type, "chance": chance}

    monkeypatch.setattr(pets.pet_service, "execute_hunt", fake_execute)
    await _start_tracking(state)
    await state.update_data(choices=["right", "cave"])

    bot = SimpleNamespace(edit_message_media=AsyncMock(side_effect=RuntimeError("gone")))
    callback = _callback("hunt_track:step3:now")
    await pets.handle_hunt_track(callback, state, bot)

    callback.message.answer.assert_awaited_once()
    assert "فرار کرد" in callback.message.answer.await_args.args[0]


async def test_track_flow_runs_the_real_hunt_service(db, state, monkeypatch):
    """The whole game with the real service: encounter → 3 clicks → roll."""
    monkeypatch.setattr(
        pets.pet_service, "roll_tracking_path", lambda **_kwargs: ["left", "river", "wait"]
    )
    # A roll far above any adjusted chance: the pet escapes.
    monkeypatch.setattr(pet_service.random, "Random", lambda: StubRng(roll=0.99))

    async def fake_encounter(session, user_id, card_power, **_kwargs):
        return {
            "status": "ok",
            "pet_type": "owl",
            "chance": pet_service.hunt_chance(card_power),
        }

    monkeypatch.setattr(pets.pet_service, "get_encounter", fake_encounter)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    bot = _bot()
    # Every click is wrong against the rolled path.
    for step, choice in ((1, "right"), (2, "cave"), (3, "now")):
        callback = _callback(f"hunt_track:step{step}:{choice}")
        await pets.handle_hunt_track(callback, state, bot)

    # Three edits in total: step 1 → step 2, step 2 → step 3, the finale.
    assert bot.edit_message_media.await_count == 3
    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.FAIL_IMAGE)
    assert await state.get_state() is None


async def test_track_flow_real_service_success_creates_the_baby(db, state, monkeypatch):
    monkeypatch.setattr(
        pets.pet_service, "roll_tracking_path", lambda **_kwargs: ["right", "cave", "now"]
    )
    monkeypatch.setattr(pet_service.random, "Random", lambda: StubRng(roll=0.0))

    async def fake_encounter(session, user_id, card_power, **_kwargs):
        return {
            "status": "ok",
            "pet_type": "owl",
            "chance": pet_service.hunt_chance(card_power),
        }

    monkeypatch.setattr(pets.pet_service, "get_encounter", fake_encounter)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    await pets.handle_hunt(message, state)

    bot = _bot()
    for step, choice in ((1, "right"), (2, "cave"), (3, "now")):
        callback = _callback(f"hunt_track:step{step}:{choice}")
        await pets.handle_hunt_track(callback, state, bot)

    assert bot.edit_message_media.await_count == 3
    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "owl_baby.jpg")
    assert await state.get_state() == PetStates.waiting_for_name.state


async def test_track_callback_is_registered_on_the_router():
    registered = [
        handler
        for handler in pets.router.callback_query.handlers
        if handler.callback is pets.handle_hunt_track
    ]
    assert registered

    event = SimpleNamespace(data="hunt_track:step1:left")
    assert all(
        [await flt.call(event, bot=None) for flt in registered[0].filters]
    ) is True


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

def test_encounter_artwork_falls_back_to_the_forest_plate(tmp_path, monkeypatch):
    """A missing adult file must not break the encounter."""
    forest = tmp_path / "hunt_forest.jpg"
    forest.write_bytes(b"placeholder")
    monkeypatch.setattr(pets, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(pets, "FOREST_IMAGE", forest)

    assert pets.adult_artwork("owl") == forest

    (tmp_path / "owl_adult.jpg").write_bytes(b"placeholder")
    assert pets.adult_artwork("owl") == tmp_path / "owl_adult.jpg"


def test_encounter_artwork_accepts_only_known_species():
    assert pets.adult_artwork("cat").name == "cat_adult.jpg"
    assert pets.baby_artwork("cat").name == "cat_baby.jpg"
    # An unknown species cannot escape the artwork directory.
    assert pets.adult_artwork("../../etc/passwd").name == f"{PET_TYPES[0]}_adult.jpg"
    assert pets.baby_artwork(None).name == f"{PET_TYPES[0]}_baby.jpg"


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


async def test_execute_hunt_uses_a_real_rng_when_none_is_injected(session, monkeypatch):
    stub = StubRng(roll=0.0, pet_type="dog")
    monkeypatch.setattr(pet_service.random, "Random", lambda: stub)

    result = await pet_service.execute_hunt(session, 1, "dog", 90, now=NOW)

    assert result["status"] == "success"
    assert len(await _pets(session)) == 1


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
