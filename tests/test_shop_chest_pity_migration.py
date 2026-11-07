"""Portable schema checks and a real Alembic 081 -> 082 -> 081 round trip."""

import importlib.util
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def _migration():
    path = ROOT / "database/migrations/versions/082_chest_pity.py"
    spec = importlib.util.spec_from_file_location("migration_082", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id BIGINT PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE shop_chests (id INTEGER PRIMARY KEY, code VARCHAR(40))")
        conn.exec_driver_sql("CREATE TABLE shop_chest_openings (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO users VALUES (1)")
        conn.exec_driver_sql("INSERT INTO shop_chests VALUES (1, 'custom_chest'), (2, 'bronze_chest')")
        conn.exec_driver_sql("INSERT INTO shop_chest_openings VALUES (7)")


def test_082_revises_081_and_is_the_single_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["082_chest_pity"]
    assert scripts.get_revision("082_chest_pity").down_revision == "081_card_rarity"


def test_upgrade_defaults_constraints_idempotence_and_history_preservation():
    engine = sa.create_engine("sqlite://")
    _baseline(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
            _migration().upgrade()
        assert conn.exec_driver_sql("SELECT code, tier FROM shop_chests ORDER BY id").all() == [
            ("custom_chest", None), ("bronze_chest", "BRONZE"),
        ]
        assert conn.exec_driver_sql("SELECT id, reward_rarity, pity_guaranteed FROM shop_chest_openings").one() == (7, None, 0)
        conn.exec_driver_sql("INSERT INTO shop_chest_pity (user_id) VALUES (1)")
        assert conn.exec_driver_sql("SELECT pity_counter FROM shop_chest_pity WHERE user_id=1").scalar_one() == 0
        conn.exec_driver_sql("UPDATE shop_chest_pity SET pity_counter=9 WHERE user_id=1")
        with pytest.raises(sa.exc.IntegrityError):
            conn.exec_driver_sql("UPDATE shop_chest_pity SET pity_counter=-1 WHERE user_id=1")
        with pytest.raises(sa.exc.IntegrityError):
            conn.exec_driver_sql("INSERT INTO shop_chest_pity (user_id) VALUES (999)")
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
        assert conn.exec_driver_sql("SELECT pity_counter FROM shop_chest_pity WHERE user_id=1").scalar_one() == 9
        with Operations.context(MigrationContext.configure(conn)):
            _migration().downgrade()
        assert not sa.inspect(conn).has_table("shop_chest_pity")
        assert conn.exec_driver_sql("SELECT id FROM shop_chest_openings").scalars().all() == [7]
        assert conn.exec_driver_sql("SELECT code FROM shop_chests ORDER BY id").scalars().all() == ["custom_chest", "bronze_chest"]
    engine.dispose()


def test_real_alembic_upgrade_and_downgrade_from_081(tmp_path):
    database = tmp_path / "chest_pity.db"
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

    alembic("stamp", "081_card_rarity")
    alembic("upgrade", "head")
    assert "082_chest_pity" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert sa.inspect(engine).has_table("shop_chest_pity")
    alembic("downgrade", "081_card_rarity")
    assert "081_card_rarity" in alembic("current")
    assert not sa.inspect(engine).has_table("shop_chest_pity")
    alembic("upgrade", "head")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT tier FROM shop_chests WHERE code='bronze_chest'").scalar_one() == "BRONZE"
        assert conn.exec_driver_sql("SELECT id FROM shop_chest_openings").scalar_one() == 7
    engine.dispose()


def test_mysql_offline_sql_contains_only_chest_schema_changes():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    with Operations.context(context):
        _migration().upgrade()
        _migration().downgrade()
    sql = output.getvalue()
    assert "CREATE TABLE shop_chest_pity" in sql and "pity_counter INTEGER NOT NULL DEFAULT '0'" in sql
    assert "reward_rarity" in sql and "pity_guaranteed" in sql
    assert "REFERENCES users (id) ON DELETE CASCADE" in sql
    assert "daily_chest" not in sql and "trades" not in sql
