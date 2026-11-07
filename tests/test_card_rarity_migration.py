"""Migration 081 reuses 012's rarity column and classifies the actual roster."""

import importlib.util
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    **dict.fromkeys(("amber", "hero", "igris"), "LEGENDARY"),
    **dict.fromkeys(("tifun", "mmung", "goje", "zarush", "filo"), "EPIC"),
    **dict.fromkeys(("nekro", "wizard", "dr_daxibe", "moon_lily", "treyni", "mahmud"), "RARE"),
    **dict.fromkeys((
        "sib_sorkh", "sib_talayi", "sib_sabz", "green_knight", "sage_king", "sib_mahi", "moon_knight",
    ), "COMMON"),
}


def _migration():
    path = ROOT / "database/migrations/versions/081_card_rarity.py"
    spec = importlib.util.spec_from_file_location("migration_081", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline(engine):
    with engine.begin() as conn:
        # Matches the pre-existing column from 012, including its raw-insert default.
        conn.execute(sa.text(
            "CREATE TABLE characters (code VARCHAR(64) PRIMARY KEY, "
            "rarity VARCHAR(20) NOT NULL DEFAULT 'COMMON', price INTEGER NOT NULL DEFAULT 300)"
        ))
        conn.execute(sa.text("CREATE TABLE shop_rotations (id INTEGER PRIMARY KEY, is_active BOOLEAN NOT NULL)"))
        conn.execute(sa.text(
            "INSERT INTO characters (code, rarity, price) VALUES (:code, 'LEGACY', 777)"
        ), [{"code": code} for code in (*EXPECTED, "custom_card")])
        conn.execute(sa.text("INSERT INTO shop_rotations VALUES (1, 1), (2, 0)"))


def _rarities(conn):
    return dict(conn.execute(sa.text("SELECT code, rarity FROM characters")).all())


def test_081_is_the_only_head_and_revises_080():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["081_card_rarity"]
    assert scripts.get_revision("081_card_rarity").down_revision == "080_group_doctors"


def test_migration_classifies_every_card_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    _baseline(engine)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
            assert _rarities(conn) == {**EXPECTED, "custom_card": "COMMON"}
            _migration().upgrade()
        assert _rarities(conn) == {**EXPECTED, "custom_card": "COMMON"}
        assert conn.execute(sa.text("SELECT price FROM characters")).scalars().all() == [777] * 22
        assert conn.execute(sa.text("SELECT id, is_active FROM shop_rotations ORDER BY id")).all() == [(1, 0), (2, 0)]
        conn.execute(sa.text("INSERT INTO characters (code) VALUES ('future_card')"))
        assert _rarities(conn)["future_card"] == "COMMON"
        with Operations.context(MigrationContext.configure(conn)):
            _migration().downgrade()
        # 080 ignores rarity; keep both the existing column and assigned data.
        assert _rarities(conn) == {**EXPECTED, "custom_card": "COMMON", "future_card": "COMMON"}
    engine.dispose()


def test_real_alembic_upgrade_from_080_and_downgrade(tmp_path):
    database = tmp_path / "rarity.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    _baseline(engine)
    engine.dispose()
    env = {**os.environ, "DATABASE_SYNC_URL": f"sqlite:///{database}"}

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=ROOT,
            env=env, text=True, capture_output=True, check=True,
        )
        return result.stdout.strip()

    alembic("stamp", "080_group_doctors")
    alembic("upgrade", "head")
    assert "081_card_rarity" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.connect() as conn:
        assert _rarities(conn) == {**EXPECTED, "custom_card": "COMMON"}
    alembic("downgrade", "080_group_doctors")
    assert "080_group_doctors" in alembic("current")
    alembic("upgrade", "head")
    engine.dispose()


def test_mysql_offline_sql_reuses_the_existing_column():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "literal_binds": True, "output_buffer": output}
    )
    with Operations.context(context):
        _migration().upgrade()
        _migration().downgrade()
    sql = output.getvalue()
    assert "UPDATE characters SET rarity='COMMON'" in sql
    assert "rarity='LEGENDARY'" in sql and "'igris'" in sql
    assert "'dr_daxibe'" in sql and "'mmung'" in sql and "'moon_lily'" in sql
    assert "UPDATE shop_rotations" in sql
    assert "ALTER TABLE" not in sql and "DROP" not in sql


def test_seed_matches_the_migration_roster():
    entries = json.loads((ROOT / "data/characters_seed.json").read_text())
    assert {entry["code"]: entry["rarity"] for entry in entries} == EXPECTED


async def test_fresh_seed_assigns_rarities_without_overwriting_operator_edits(db_session):
    from sib.database.connection import seed_characters
    from sib.database.models import Character
    from sib.services.characters import CharacterService

    assert await seed_characters(db_session) == 21
    characters = list((await db_session.scalars(sa.select(Character))).all())
    assert {character.code: character.rarity for character in characters} == EXPECTED
    igris = next(character for character in characters if character.code == "igris")
    await CharacterService.update(db_session, igris.id, rarity="RARE")
    assert await seed_characters(db_session) == 0
    assert igris.rarity == "RARE"
