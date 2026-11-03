"""Pet System phase 5: the one-time paid rename (✏️ تغییر نام).

Three layers are covered:

* migration ``077_pet_rename`` adds ``user_pets.has_renamed`` (the single head
  is now ``078_wanted_decay``);
* :func:`sib.services.pet_service.rename_pet` owns every rule (ownership, the
  once-per-pet limit, 2-20 characters, the 500-apple price);
* :mod:`sib.handlers.pets` only renders the result — including the strict
  reply-to-prompt guard shared with the naming flow.
"""

import importlib.util
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    SystemConfig,
    User,
    UserPet,
)
from sib.handlers import pets
from sib.handlers.pets import PetStates
from sib.services import pet_service
from sib.services.apple_wallet import AppleWalletService
from sib.services.sib_claim import fa_digits

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/077_pet_rename.py"

PROMPT_MESSAGE_ID = 777
RENAME_COST = 500


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _migration():
    spec = importlib.util.spec_from_file_location("migration_077_pet_rename", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine, direction):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(), direction)()


@pytest.fixture
def migration_engine():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE user_pets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "pet_type VARCHAR(32) NOT NULL, "
                "custom_name VARCHAR(64), "
                "is_adult BOOLEAN NOT NULL DEFAULT 0, "
                "xp INTEGER NOT NULL DEFAULT 0, "
                "skill_level INTEGER NOT NULL DEFAULT 1, "
                "last_fed_at DATETIME, "
                "last_hunted_at DATETIME, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        User.__table__,
        SystemConfig.__table__,
        UserPet.__table__,
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
    async with factory.begin() as db_session:
        db_session.add(User(id=1, first_name="rename player"))
        db_session.add(User(id=2, first_name="other player"))
    async with factory() as db_session:
        yield db_session


@pytest_asyncio.fixture
async def db(engine, monkeypatch):
    """Session factory shared with the handlers through ``pets.get_session``."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def get_session():
        async with factory.begin() as db_session:
            yield db_session

    monkeypatch.setattr(pets, "get_session", get_session)
    async with factory.begin() as db_session:
        db_session.add(User(id=1, first_name="rename player"))
        db_session.add(User(id=2, first_name="other player"))
    yield factory


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _seed_pet(db, *, user_id: int = 1, custom_name="ملوس", has_renamed=False,
                    apples: int = 0) -> int:
    async with db.begin() as db_session:
        pet = UserPet(
            user_id=user_id,
            pet_type="cat",
            custom_name=custom_name,
            has_renamed=has_renamed,
        )
        db_session.add(pet)
        await db_session.flush()
        pet_id = pet.id
    if apples:
        async with db.begin() as db_session:
            await AppleWalletService.earn(
                db_session, user_id, apples, reason="test_grant"
            )
    return pet_id


async def _wallet(session, user_id: int = 1) -> int:
    return await AppleWalletService.get_balance(session, user_id)


async def _pet(session, pet_id: int) -> UserPet:
    return await session.get(UserPet, pet_id)


# ---------------------------------------------------------------------------
# Migration 077
# ---------------------------------------------------------------------------

def test_077_migration_revises_076_and_078_is_the_single_head():
    migration = _migration()
    assert migration.revision == "077_pet_rename"
    assert migration.down_revision == "076_pet_system"

    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["078_wanted_decay"]
    assert (
        scripts.get_revision("077_pet_rename").down_revision == "076_pet_system"
    )


def test_077_upgrade_adds_the_flag_and_downgrade_removes_it(migration_engine):
    engine = migration_engine
    _run_migration(engine, "upgrade")

    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("user_pets")}
    assert "has_renamed" in columns
    assert columns["has_renamed"]["nullable"] is False

    # Existing pets read as "not renamed yet", i.e. the one rename is available.
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO users (id) VALUES (1)")
        )
        connection.execute(
            sa.text("INSERT INTO user_pets (user_id, pet_type) VALUES (1, 'cat')")
        )
        assert connection.execute(
            sa.text("SELECT has_renamed FROM user_pets")
        ).scalar() in (0, False)

    # Idempotent: running it twice is a no-op.
    _run_migration(engine, "upgrade")
    assert "has_renamed" in {
        c["name"] for c in sa.inspect(engine).get_columns("user_pets")
    }

    _run_migration(engine, "downgrade")
    assert "has_renamed" not in {
        c["name"] for c in sa.inspect(engine).get_columns("user_pets")
    }


def test_alembic_real_upgrade_through_078(tmp_path):
    database = tmp_path / "pet_rename.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )
    engine.dispose()
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{database}"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT.parent), str(ROOT), env.get("PYTHONPATH", ""))
    )

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert alembic("heads") == "078_wanted_decay (head)"
    # 076 creates ``user_pets`` itself, so the synthetic baseline starts at its
    # parent and the whole pet chain (076 create, 077 alter, 078 seed) really runs.
    alembic("stamp", "075_rocket_game")
    alembic("upgrade", "head")
    assert "078_wanted_decay" in alembic("current")

    engine = sa.create_engine(f"sqlite:///{database}")
    assert "has_renamed" in {
        c["name"] for c in sa.inspect(engine).get_columns("user_pets")
    }
    engine.dispose()


def test_user_pet_model_has_the_rename_flag():
    column = UserPet.__table__.c.has_renamed
    assert column.nullable is False
    assert UserPet().has_renamed in (False, None)  # default applies on insert


# ---------------------------------------------------------------------------
# Service — rename_pet
# ---------------------------------------------------------------------------

async def test_rename_pet_charges_500_and_updates_the_name(session):
    pet_id = None
    session.add(UserPet(user_id=1, pet_type="cat", custom_name="ملوس"))
    await session.flush()
    pet_id = (
        await session.scalar(sa.select(UserPet.id).where(UserPet.user_id == 1))
    )
    await AppleWalletService.earn(session, 1, 900, reason="test_grant")

    result = await pet_service.rename_pet(session, 1, pet_id, "  پشمک  ")

    assert result["status"] == "renamed"
    assert result["name"] == "پشمک"
    assert result["cost"] == RENAME_COST
    assert await _wallet(session) == 900 - RENAME_COST

    pet = await _pet(session, pet_id)
    assert pet.custom_name == "پشمک"
    assert pet.has_renamed is True

    # Exactly one ledger entry, for the rename reason.
    debit = await session.scalar(
        sa.select(AppleTransaction).where(
            AppleTransaction.reason == pet_service.RENAME_LEDGER_REASON
        )
    )
    assert debit is not None
    assert debit.amount == -RENAME_COST


async def test_rename_pet_refuses_without_enough_apples(session):
    session.add(UserPet(user_id=1, pet_type="cat", custom_name="ملوس"))
    await session.flush()
    pet_id = await session.scalar(sa.select(UserPet.id))
    await AppleWalletService.earn(session, 1, RENAME_COST - 1, reason="test_grant")

    result = await pet_service.rename_pet(session, 1, pet_id, "پشمک")

    assert result["status"] == "insufficient_funds"
    assert result["cost"] == RENAME_COST
    assert await _wallet(session) == RENAME_COST - 1
    pet = await _pet(session, pet_id)
    assert pet.custom_name == "ملوس"
    assert pet.has_renamed is False


async def test_rename_pet_is_allowed_only_once(session):
    session.add(
        UserPet(user_id=1, pet_type="cat", custom_name="ملوس", has_renamed=True)
    )
    await session.flush()
    pet_id = await session.scalar(sa.select(UserPet.id))
    await AppleWalletService.earn(session, 1, 5000, reason="test_grant")

    result = await pet_service.rename_pet(session, 1, pet_id, "پشمک")

    assert result["status"] == "already_renamed"
    assert await _wallet(session) == 5000  # nothing was charged
    assert (await _pet(session, pet_id)).custom_name == "ملوس"


async def test_rename_pet_cannot_reuse_the_paid_debit(session):
    """A stale flag must not buy a second rename with the same ledger key."""
    session.add(UserPet(user_id=1, pet_type="cat", custom_name="ملوس"))
    await session.flush()
    pet_id = await session.scalar(sa.select(UserPet.id))
    await AppleWalletService.earn(session, 1, 2000, reason="test_grant")

    first = await pet_service.rename_pet(session, 1, pet_id, "پشمک")
    assert first["status"] == "renamed"

    # Simulate a stale read: the flag is cleared behind the service's back.
    pet = await _pet(session, pet_id)
    pet.has_renamed = False
    await session.flush()

    second = await pet_service.rename_pet(session, 1, pet_id, "خرس")
    assert second["status"] == "already_renamed"
    assert await _wallet(session) == 2000 - RENAME_COST
    assert (await _pet(session, pet_id)).custom_name == "پشمک"


async def test_rename_pet_refuses_another_players_pet(session):
    session.add(UserPet(user_id=2, pet_type="dog", custom_name="rex"))
    await session.flush()
    pet_id = await session.scalar(sa.select(UserPet.id))
    await AppleWalletService.earn(session, 1, 5000, reason="test_grant")

    result = await pet_service.rename_pet(session, 1, pet_id, "پشمک")

    assert result["status"] == "not_found"
    assert await _wallet(session) == 5000
    assert (await _pet(session, pet_id)).custom_name == "rex"


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("", "empty"),
        ("  ", "empty"),
        ("x", "too_short"),
        ("x" * 21, "too_long"),
        ("<b>x</b>", "invalid_chars"),
        ("پت 🐺", "invalid_chars"),
        ("۱۲۳", "digits_only"),
    ],
)
async def test_rename_pet_reports_the_validation_reason(session, raw, reason):
    session.add(UserPet(user_id=1, pet_type="cat", custom_name="ملوس"))
    await session.flush()
    pet_id = await session.scalar(sa.select(UserPet.id))
    await AppleWalletService.earn(session, 1, 5000, reason="test_grant")

    result = await pet_service.rename_pet(session, 1, pet_id, raw)

    assert result["status"] == reason
    assert await _wallet(session) == 5000  # an invalid name is free
    pet = await _pet(session, pet_id)
    assert pet.custom_name == "ملوس"
    assert pet.has_renamed is False


@pytest.mark.parametrize("bad_id", ["abc", None])
async def test_rename_pet_refuses_a_bad_pet_id(session, bad_id):
    result = await pet_service.rename_pet(session, 1, bad_id, "پشمک")
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Handlers — the rename button and the reply guard
# ---------------------------------------------------------------------------

def _callback(data: str, *, clicker_id: int = 1, message_id: int = 5):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=clicker_id, is_bot=False),
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=1),
            photo=[SimpleNamespace(file_id="f")],
            answer=AsyncMock(
                return_value=SimpleNamespace(message_id=PROMPT_MESSAGE_ID)
            ),
            answer_photo=AsyncMock(),
        ),
    )


def _bot():
    return SimpleNamespace(
        edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )


def _message(text: str, *, reply_to=PROMPT_MESSAGE_ID, user_id: int = 1):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        text=text,
        reply_to_message=(
            None if reply_to is None else SimpleNamespace(message_id=reply_to)
        ),
        reply=AsyncMock(),
    )


def test_rename_button_label_and_color():
    pet = SimpleNamespace(id=9, is_adult=False, has_renamed=False)

    keyboard = pets.pet_profile_keyboard(pet)
    by_callback = {
        button.callback_data: button
        for row in keyboard.inline_keyboard
        for button in row
    }

    rename = by_callback["pet_rename:9"]
    assert rename.text == f"✏️ تغییر نام ({fa_digits(RENAME_COST)} سیب)"
    assert rename.style == "primary"
    # The other buttons of the same screen keep their colors.
    assert by_callback["pet_feed:9"].style == "success"
    assert by_callback["pet_view:menu"].style == "danger"


def test_every_pet_button_is_colored():
    styles = {"primary", "success", "danger"}

    for keyboard in (
        pets.pet_profile_keyboard(
            SimpleNamespace(id=1, is_adult=False, has_renamed=False)
        ),
        pets.pet_profile_keyboard(
            SimpleNamespace(id=1, is_adult=True, has_renamed=True)
        ),
        pets.pet_menu_keyboard(
            [SimpleNamespace(id=1, pet_type="cat", custom_name="ملوس")]
        ),
        pets.tracking_keyboard(2),
    ):
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        assert buttons
        assert {button.style for button in buttons} <= styles

    # An adult pet that already used its rename keeps only the back button.
    adult = pets.pet_profile_keyboard(
        SimpleNamespace(id=1, is_adult=True, has_renamed=True)
    )
    assert [
        button.callback_data for row in adult.inline_keyboard for button in row
    ] == ["pet_view:menu"]


async def test_rename_button_is_hidden_after_the_rename(db, state):
    pet_id = await _seed_pet(db, has_renamed=True, apples=RENAME_COST)

    callback = _callback(f"pet_view:{pet_id}")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    keyboard = bot.edit_message_media.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callbacks == [f"pet_feed:{pet_id}", "pet_view:menu"]


async def test_rename_callback_asks_for_a_reply_and_sets_the_state(db, state):
    pet_id = await _seed_pet(db, has_renamed=False)

    callback = _callback(f"pet_rename:{pet_id}")
    await pets.handle_pet_rename(callback, state, _bot())

    callback.message.answer.assert_awaited_once_with(pets.RENAME_PROMPT)
    data = await state.get_data()
    assert await state.get_state() == PetStates.waiting_for_rename.state
    assert data["pet_id"] == pet_id
    assert data["prompt_message_id"] == PROMPT_MESSAGE_ID


async def test_rename_callback_refuses_another_players_pet(db, state):
    pet_id = await _seed_pet(db, user_id=2)

    callback = _callback(f"pet_rename:{pet_id}")
    await pets.handle_pet_rename(callback, state, _bot())

    assert callback.answer.await_args.args[0] == pets.PET_GONE_ALERT
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert await state.get_state() is None


async def test_rename_callback_refuses_a_pet_that_already_used_it(db, state):
    pet_id = await _seed_pet(db, has_renamed=True)

    callback = _callback(f"pet_rename:{pet_id}")
    await pets.handle_pet_rename(callback, state, _bot())

    assert callback.answer.await_args.args[0] == pets.RENAME_ALREADY_USED_ALERT
    assert callback.answer.await_args.kwargs["show_alert"] is True
    callback.message.answer.assert_not_awaited()
    assert await state.get_state() is None


async def test_rename_flow_renames_and_clears_the_state(db, state):
    pet_id = await _seed_pet(db, has_renamed=False, apples=RENAME_COST)
    callback = _callback(f"pet_rename:{pet_id}")
    await pets.handle_pet_rename(callback, state, _bot())

    message = _message("پشمک")
    await pets.handle_pet_rename_name(message, state)

    message.reply.assert_awaited_once()
    assert "پشمک" in message.reply.await_args.args[0]
    assert await state.get_state() is None
    assert await state.get_data() == {}

    async with db.begin() as db_session:
        pet = await _pet(db_session, pet_id)
        assert pet.custom_name == "پشمک"
        assert pet.has_renamed is True
        assert await _wallet(db_session) == 0


async def test_rename_handler_without_a_reply_is_ignored(db, state):
    """The guard: a message that does not reply to the prompt is not a name."""
    pet_id = await _seed_pet(db, has_renamed=False, apples=RENAME_COST)
    await state.set_state(PetStates.waiting_for_rename)
    await state.update_data(pet_id=pet_id, prompt_message_id=PROMPT_MESSAGE_ID)

    # No reply at all (a friend typing in the group), and a reply to a
    # different message.
    for reply_to in (None, PROMPT_MESSAGE_ID + 1):
        message = _message("اسم الکی", reply_to=reply_to)
        await pets.handle_pet_rename_name(message, state)

        message.reply.assert_awaited_once_with(pets.REPLY_TO_PROMPT_REQUIRED)
        assert await state.get_state() == PetStates.waiting_for_rename.state

    async with db.begin() as db_session:
        pet = await _pet(db_session, pet_id)
        assert pet.custom_name == "ملوس"  # untouched
        assert pet.has_renamed is False
        assert await _wallet(db_session) == RENAME_COST  # nothing charged


async def test_rename_handler_keeps_the_state_without_enough_apples(db, state):
    pet_id = await _seed_pet(db, has_renamed=False, apples=10)
    await state.set_state(PetStates.waiting_for_rename)
    await state.update_data(pet_id=pet_id, prompt_message_id=PROMPT_MESSAGE_ID)

    message = _message("پشمک")
    await pets.handle_pet_rename_name(message, state)

    assert message.reply.await_args.args[0] == pets.RENAME_NO_FUNDS_ALERT
    assert await state.get_state() == PetStates.waiting_for_rename.state
    async with db.begin() as db_session:
        assert (await _pet(db_session, pet_id)).custom_name == "ملوس"


async def test_rename_handler_rejects_an_invalid_name_and_keeps_the_state(db, state):
    pet_id = await _seed_pet(db, has_renamed=False, apples=RENAME_COST)
    await state.set_state(PetStates.waiting_for_rename)
    await state.update_data(pet_id=pet_id, prompt_message_id=PROMPT_MESSAGE_ID)

    message = _message("x")
    await pets.handle_pet_rename_name(message, state)

    assert message.reply.await_args.args[0] == pets.NAME_ERRORS["too_short"]
    assert await state.get_state() == PetStates.waiting_for_rename.state


async def test_rename_handler_without_a_pet_id_asks_to_hunt(db, state):
    await state.set_state(PetStates.waiting_for_rename)
    await state.update_data(prompt_message_id=PROMPT_MESSAGE_ID)

    message = _message("پشمک")
    await pets.handle_pet_rename_name(message, state)

    message.reply.assert_awaited_once_with(pets.NO_PET_TEXT)
    assert await state.get_state() is None


def test_rename_handlers_are_registered_on_the_router():
    callbacks = [h.callback for h in pets.router.callback_query.handlers]
    messages = [h.callback for h in pets.router.message.handlers]
    assert pets.handle_pet_rename in callbacks
    assert pets.handle_pet_rename_name in messages


# ---------------------------------------------------------------------------
# The same guard protects the naming flow
# ---------------------------------------------------------------------------

async def test_naming_handler_without_a_reply_is_ignored(db, state):
    async with db.begin() as db_session:
        pet = UserPet(user_id=1, pet_type="cat", is_adult=False)
        db_session.add(pet)
        await db_session.flush()
        pet_id = pet.id

    await state.set_state(PetStates.waiting_for_name)
    await state.update_data(pet_id=pet_id, prompt_message_id=PROMPT_MESSAGE_ID)

    for reply_to in (None, PROMPT_MESSAGE_ID + 1):
        message = _message("پیام دوستم", reply_to=reply_to)
        await pets.handle_pet_name(message, state)

        message.reply.assert_awaited_once_with(pets.REPLY_TO_PROMPT_REQUIRED)
        assert await state.get_state() == PetStates.waiting_for_name.state

    async with db.begin() as db_session:
        assert (await _pet(db_session, pet_id)).custom_name is None
