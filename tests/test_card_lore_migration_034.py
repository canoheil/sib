"""Migration 034: additive schema, exact text and data-preserving rollback."""

import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models import Character

MIGRATION = Path(__file__).resolve().parents[1] / "database/migrations/versions/034_card_affiliations.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_034", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_load(), direction)()


def test_034_revision_and_nullable_text_match_the_model():
    migration = _load()
    assert migration.revision == "034_card_affiliations"
    assert migration.down_revision == "033_shop_card_price"
    assert len(migration.revision) <= 32
    column = Character.__table__.c.affiliations_fa
    assert column.nullable and isinstance(column.type, sa.Text)


def test_034_only_adds_a_column_and_preserves_existing_rows():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE characters (id INTEGER PRIMARY KEY, name_fa TEXT, lore_fa TEXT, power INTEGER)"
        ))
        connection.execute(sa.text(
            "INSERT INTO characters VALUES (1, 'سیب سرخ', 'داستان قبلی', 22), (2, 'کارت دیگر', 'عین متن', 7)"
        ))
        connection.execute(sa.text(
            "CREATE TABLE player_cards (id INTEGER PRIMARY KEY, character_id INTEGER, level INTEGER, is_favorite INTEGER)"
        ))
        connection.execute(sa.text("INSERT INTO player_cards VALUES (3, 1, 8, 1)"))
        connection.execute(sa.text("CREATE TABLE apple_wallets (user_id INTEGER, balance INTEGER)"))
        connection.execute(sa.text("INSERT INTO apple_wallets VALUES (4, 1234)"))
        characters = connection.execute(sa.text("SELECT * FROM characters ORDER BY id")).all()
        cards = connection.execute(sa.text("SELECT * FROM player_cards")).all()
        wallets = connection.execute(sa.text("SELECT * FROM apple_wallets")).all()
        _run(connection, "upgrade")
        assert connection.execute(sa.text(
            "SELECT id, name_fa, lore_fa, power FROM characters ORDER BY id"
        )).all() == characters
        assert connection.execute(sa.text("SELECT affiliations_fa FROM characters")).all() == [(None,), (None,)]
        assert connection.execute(sa.text("SELECT * FROM player_cards")).all() == cards
        assert connection.execute(sa.text("SELECT * FROM apple_wallets")).all() == wallets
        # An idempotent retry must not clear text already stored.
        exact = "امپراتوری👑 - تیغ های تاج🗡  \n"
        connection.execute(sa.text("UPDATE characters SET affiliations_fa = :text WHERE id = 1"), {"text": exact})
        _run(connection, "upgrade")
        assert connection.scalar(sa.text("SELECT affiliations_fa FROM characters WHERE id = 1")) == exact
        # Application rollback cannot delete the official text either.
        _run(connection, "downgrade")
        _run(connection, "upgrade")
        assert connection.scalar(sa.text("SELECT affiliations_fa FROM characters WHERE id = 1")) == exact
    engine.dispose()


def test_034_offline_mariadb_sql_is_additive_only():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        _load().upgrade()
    sql = output.getvalue().upper()
    assert "ALTER TABLE CHARACTERS ADD COLUMN AFFILIATIONS_FA TEXT" in sql
    assert "NOT NULL" not in sql
    for operation in ("UPDATE ", "DELETE ", "DROP ", "TRUNCATE "):
        assert operation not in sql
