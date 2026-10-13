"""Additive 035 upgrade validated on an isolated local SQLite schema."""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sib.database.models import Base
from sqlalchemy import create_engine, inspect, text


def migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "database/migrations/versions/035_apple_duel.py"
    )
    spec = importlib.util.spec_from_file_location("duel_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_existing_data_safe_and_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        tables = [
            t
            for t in Base.metadata.sorted_tables
            if not t.name.startswith("apple_duel")
        ]
        Base.metadata.create_all(conn, tables=tables)
        conn.execute(
            text(
                "INSERT INTO users(id,first_name,is_bot,is_active,language_code,profile_skin) "
                "VALUES (501,'قدیمی',0,1,'fa','green_apple')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO apple_battles(id,player1_id,player2_id,player1_card_id,player2_card_id,"
                "level,status,result_version,seedlings_awarded) VALUES (51,501,502,1,2,1,'COMPLETED',1,1)"
            )
        )
        migration = migration_module()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
        assert conn.scalar(text("SELECT first_name FROM users WHERE id=501")) == "قدیمی"
        assert conn.scalar(text("SELECT COUNT(*) FROM apple_battles")) == 1
        inspector = inspect(conn)
        assert {"apple_duels", "apple_duel_slots"} <= set(inspector.get_table_names())
        assert inspector.get_pk_constraint("apple_duel_slots")[
            "constrained_columns"
        ] == ["user_id"]
        assert {c["name"] for c in inspector.get_columns("apple_duels")} == set(
            Base.metadata.tables["apple_duels"].columns.keys()
        )
        assert (
            inspector.get_foreign_keys("apple_duels")[0]["referred_table"]
            == "apple_battles"
        )
    engine.dispose()


def test_offline_mysql_ddl_contains_constraints():
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        migration_module().upgrade()
    sql = buffer.getvalue()
    assert "CREATE TABLE apple_duels" in sql and "CREATE TABLE apple_duel_slots" in sql
    assert (
        "UNIQUE (battle_id)" in sql
        and "FOREIGN KEY(battle_id) REFERENCES apple_battles" in sql
    )
    assert "DROP" not in sql and "DELETE" not in sql
