"""Pet System phase 1: schema, model, config seed and artwork.

Backend only: no handlers, commands or menus exist for pets yet.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import PET_TYPES, SystemConfig, User, UserPet

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/076_pet_system.py"
PET_ASSETS = ROOT / "assets/pets"
EXPECTED_CONFIG = {
    "pet_hunt_cooldown_hours": 2,
    "pet_feed_xp_amount": 50,
    "pet_adult_xp_required": 500,
    "pet_skill_max_level": 3,
    "pet_skill_upgrade_diamond_cost": 10,
}
EXPECTED_SEED = {key: str(value) for key, value in EXPECTED_CONFIG.items()}
EXPECTED_ARTWORK = {
    f"{pet_type}_{stage}.jpg": (pet_type, stage)
    for pet_type in ("dog", "squirrel", "cat", "owl")
    for stage in ("baby", "adult")
}
#: Encounter plates used by the «سیب شکار» mini-game (Phase 2.5); they are
#: full-bleed scenery, not a species/stage pair.
EXPECTED_SCENERY = {"hunt_forest.jpg", "hunt_fail.jpg"}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _migration():
    spec = importlib.util.spec_from_file_location("migration_076_pet_system", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine, direction):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(), direction)()


def _config_rows(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
        )


def _foreign_keys_on(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _make_migration_baseline(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE characters (code VARCHAR(64) PRIMARY KEY, rarity VARCHAR(20) NOT NULL DEFAULT 'COMMON')"))
        connection.execute(sa.text("CREATE TABLE shop_rotations (is_active BOOLEAN NOT NULL)"))
        connection.execute(sa.text("CREATE TABLE shop_chests (id INTEGER PRIMARY KEY, code VARCHAR(40))"))
        connection.execute(sa.text("CREATE TABLE shop_chest_openings (id INTEGER PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )


@pytest.fixture
def migration_engine():
    engine = sa.create_engine("sqlite:///:memory:")
    sa.event.listen(engine, "connect", _foreign_keys_on)
    _make_migration_baseline(engine)
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sa.event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    tables = [User.__table__, SystemConfig.__table__, UserPet.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: sa.MetaData().create_all(
                sync_connection, tables=tables
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


# ---------------------------------------------------------------------------
# Migration 076
# ---------------------------------------------------------------------------

def test_076_migration_revises_075_and_079_is_the_single_head():
    migration = _migration()
    assert migration.revision == "076_pet_system"
    assert migration.down_revision == "075_rocket_game"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["082_chest_pity"]
    assert scripts.get_revision("077_pet_rename").down_revision == "076_pet_system"
    assert scripts.get_revision("076_pet_system").down_revision == "075_rocket_game"


def test_config_defaults_match_the_migration_seed():
    assert EXPECTED_SEED == {
        "pet_hunt_cooldown_hours": "2",
        "pet_feed_xp_amount": "50",
        "pet_adult_xp_required": "500",
        "pet_skill_max_level": "3",
        "pet_skill_upgrade_diamond_cost": "10",
    }
    assert _migration()._CONFIG_DEFAULTS == EXPECTED_SEED
    # The seeded values are the integers the service layer will read back.
    assert {key: int(value) for key, value in EXPECTED_SEED.items()} == EXPECTED_CONFIG


def test_migration_upgrade_creates_table_seeds_and_preserves_overrides(migration_engine):
    engine = migration_engine
    _run_migration(engine, "upgrade")

    inspector = sa.inspect(engine)
    assert inspector.has_table("user_pets")
    assert {column["name"] for column in inspector.get_columns("user_pets")} == {
        "id",
        "user_id",
        "pet_type",
        "custom_name",
        "is_adult",
        "xp",
        "skill_level",
        "last_fed_at",
        "last_hunted_at",
        "created_at",
    }
    assert [index["name"] for index in inspector.get_indexes("user_pets")] == [
        "ix_user_pets_user_id"
    ]
    assert _config_rows(engine) == EXPECTED_SEED

    # The seed is idempotent: a second upgrade neither fails nor overwrites.
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE system_config SET `value` = '9' WHERE `key` = :key"),
            {"key": "pet_skill_max_level"},
        )
    _run_migration(engine, "upgrade")
    rows = _config_rows(engine)
    assert rows["pet_skill_max_level"] == "9"
    assert rows["pet_adult_xp_required"] == "500"

    _run_migration(engine, "downgrade")
    assert not sa.inspect(engine).has_table("user_pets")
    # Operator override survives; untouched defaults are removed.
    assert _config_rows(engine) == {"pet_skill_max_level": "9"}


def test_migration_enforces_user_foreign_key(migration_engine):
    engine = migration_engine
    _run_migration(engine, "upgrade")
    with engine.begin() as connection:
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO user_pets (user_id, pet_type) VALUES (999, 'dog')"
                )
            )


def test_alembic_real_upgrade_from_075(tmp_path):
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["082_chest_pity"]

    database = tmp_path / "pet_migration.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    _make_migration_baseline(engine)
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

    assert alembic("heads") == "082_chest_pity (head)"
    alembic("stamp", "075_rocket_game")
    alembic("upgrade", "head")
    assert "082_chest_pity" in alembic("current")

    engine = sa.create_engine(f"sqlite:///{database}")
    assert sa.inspect(engine).has_table("user_pets")
    # 078_wanted_decay and 079_disease_daily_announcement run too and seed their own keys alongside 076.
    assert _config_rows(engine) == {
        **EXPECTED_SEED,
        "theft_star_decay_hours": "3",
        "disease_announcement_hour": "18",
        "disease_announcement_minute": "15",
        "disease_announcement_enabled": "true",
    }
    engine.dispose()


# ---------------------------------------------------------------------------
# UserPet model (basic CRUD)
# ---------------------------------------------------------------------------

async def _add_user(session, user_id: int, name: str = "بازیکن") -> User:
    session.add(User(id=user_id, first_name=name))
    await session.flush()
    return await session.get(User, user_id)


async def test_user_pet_model_crud_and_defaults(session):
    user = await _add_user(session, 42)

    pet = UserPet(user_id=user.id, pet_type="cat")
    session.add(pet)
    await session.commit()
    await session.refresh(pet)

    assert pet.id is not None
    assert pet.custom_name is None  # unnamed until the owner names it
    assert pet.is_adult is False
    assert pet.xp == 0
    assert pet.skill_level == 1
    assert pet.last_fed_at is None
    assert pet.last_hunted_at is None
    assert pet.created_at is not None

    pet.custom_name = "ملوس"
    pet.xp = 500
    pet.is_adult = True
    pet.skill_level = 2
    await session.commit()

    reloaded = await session.get(UserPet, pet.id)
    assert reloaded.custom_name == "ملوس"
    assert reloaded.xp == 500
    assert reloaded.is_adult is True
    assert reloaded.skill_level == 2

    await session.delete(reloaded)
    await session.commit()
    assert await session.get(UserPet, pet.id) is None


async def test_user_pet_supports_many_pets_and_cascade_delete(session):
    user = await _add_user(session, 7)
    session.add_all(
        [
            UserPet(user_id=user.id, pet_type=pet_type, custom_name=pet_type)
            for pet_type in PET_TYPES
        ]
    )
    await session.commit()

    pets = (
        await session.scalars(
            sa.select(UserPet).where(UserPet.user_id == user.id).order_by(UserPet.id)
        )
    ).all()
    assert [pet.pet_type for pet in pets] == list(PET_TYPES)

    # users.id -> user_pets.user_id is ON DELETE CASCADE.
    await session.delete(user)
    await session.commit()
    assert (
        await session.scalars(sa.select(UserPet).where(UserPet.user_id == 7))
    ).all() == []


def test_user_pet_columns_are_indexed_and_nullable_as_specified():
    columns = UserPet.__table__.c
    assert UserPet.__tablename__ == "user_pets"
    assert columns.id.primary_key
    assert columns.user_id.index is True
    assert columns.pet_type.nullable is False
    assert columns.custom_name.nullable is True
    assert columns.is_adult.nullable is False
    assert columns.xp.nullable is False
    assert columns.skill_level.nullable is False
    assert columns.last_fed_at.nullable is True
    assert columns.last_hunted_at.nullable is True
    assert columns.has_renamed.nullable is False
    assert columns.created_at.nullable is False
    assert columns.created_at.type.timezone is True


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", sorted(EXPECTED_ARTWORK) + sorted(EXPECTED_SCENERY))
def test_pet_artwork_is_a_full_bleed_1024_square_jpeg(filename):
    path = PET_ASSETS / filename
    assert path.is_file(), f"missing pet artwork: {path}"

    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (1024, 1024)
        assert image.mode == "RGB"
        image.load()
        rgb = image.convert("RGB")

    # Full-bleed: painted scenery reaches every edge, so no border pixel may
    # be near-neutral white (a paper margin or frame would be R == G == B ~255).
    width, height = rgb.size
    border = (
        [rgb.getpixel((x, 0)) for x in range(0, width, 8)]
        + [rgb.getpixel((x, height - 1)) for x in range(0, width, 8)]
        + [rgb.getpixel((0, y)) for y in range(0, height, 8)]
        + [rgb.getpixel((width - 1, y)) for y in range(0, height, 8)]
    )
    white_edge_pixels = [
        pixel
        for pixel in border
        if min(pixel) > 230 and max(pixel) - min(pixel) < 12
    ]
    assert not white_edge_pixels, (
        f"{filename} has a white border/margin "
        f"({len(white_edge_pixels)}/{len(border)} edge pixels)"
    )


def test_pet_artwork_covers_every_species_and_stage():
    on_disk = {path.name for path in PET_ASSETS.glob("*.jpg")}
    assert on_disk == set(EXPECTED_ARTWORK) | EXPECTED_SCENERY
    assert len(EXPECTED_ARTWORK) == 8
    assert {pet_type for pet_type, _ in EXPECTED_ARTWORK.values()} == set(PET_TYPES)
    assert EXPECTED_SCENERY.isdisjoint(EXPECTED_ARTWORK)
