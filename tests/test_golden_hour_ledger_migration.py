"""Migration 068 persists the applied golden-hour reward multiplier."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models.apple import AppleTransaction

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "database/migrations/versions/068_golden_hour_apple_rewards.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("migration_068", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(connection, direction: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), direction)()


def test_ledger_model_defaults_multiplier_to_one():
    column = AppleTransaction.__table__.c.reward_multiplier
    assert isinstance(column.type, sa.SmallInteger)
    assert column.nullable is False
    assert column.default is not None and column.default.arg == 1
    assert column.server_default is not None
    assert str(column.server_default.arg) == "1"


def test_multiplier_migration_defaults_existing_and_new_ledger_rows_to_one():
    migration = _migration()
    assert migration.revision == "068_golden_hour_apple_rewards"
    assert migration.down_revision == "067_invasion_daily_20_schedule"

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE apple_transactions ("
                "id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO apple_transactions (id, amount) VALUES (1, 5)")
        )

        _run(connection, "upgrade")
        columns = {
            row["name"]: row for row in sa.inspect(connection).get_columns("apple_transactions")
        }
        assert isinstance(columns["reward_multiplier"]["type"], sa.SmallInteger)
        assert columns["reward_multiplier"]["nullable"] is False
        assert connection.scalar(
            sa.text("SELECT reward_multiplier FROM apple_transactions WHERE id = 1")
        ) == 1

        # The server default protects old and direct SQL writers that omit the
        # new field; the ORM also supplies the same default in the model.
        connection.execute(
            sa.text("INSERT INTO apple_transactions (id, amount) VALUES (2, 10)")
        )
        assert connection.scalar(
            sa.text("SELECT reward_multiplier FROM apple_transactions WHERE id = 2")
        ) == 1

        _run(connection, "downgrade")
        remaining = {
            row["name"] for row in sa.inspect(connection).get_columns("apple_transactions")
        }
        assert "reward_multiplier" not in remaining
    engine.dispose()
