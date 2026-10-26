"""Migration 067 changes only the old invasion schedule default."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "database/migrations/versions/067_invasion_daily_20_schedule.py"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_067", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(connection, direction: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), direction)()


@pytest.mark.parametrize(
    ("initial", "after_upgrade", "after_downgrade"),
    [
        ("0,12,16,20", '"20"', "0,12,16,20"),
        ('"0,12,16,20"', '"20"', "0,12,16,20"),
        ("9,21", "9,21", "9,21"),
    ],
)
def test_migration_only_replaces_the_untouched_default(initial, after_upgrade, after_downgrade):
    migration = _migration()
    assert migration.revision == "067_invasion_daily_20_schedule"
    assert migration.down_revision == "066_daily_channel_digest"

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO system_config (`key`, `value`) VALUES (:key, :value)"),
            {"key": "invasion_spawn_hours", "value": initial},
        )
        connection.execute(
            sa.text("INSERT INTO system_config (`key`, `value`) VALUES (:key, :value)"),
            {"key": "daily_digest_hour", "value": "20"},
        )

        _run(connection, "upgrade")
        _run(connection, "upgrade")  # safe if accidentally applied twice
        values = dict(connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all())
        assert values["invasion_spawn_hours"] == after_upgrade
        assert values["daily_digest_hour"] == "20"

        _run(connection, "downgrade")
        values = dict(connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all())
        assert values["invasion_spawn_hours"] == after_downgrade
        assert values["daily_digest_hour"] == "20"
    engine.dispose()
