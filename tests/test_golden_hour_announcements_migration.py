"""Schema contract for migration 069_golden_hour_notices."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models.golden_hour_announcement import GoldenHourGroupAnnouncement


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "database/migrations/versions/069_golden_hour_notices.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("migration_069", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(connection, direction: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), direction)()


def test_upgrade_creates_model_compatible_once_per_group_day_table():
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE groups (id BIGINT PRIMARY KEY)")
            )
            _run(connection, "upgrade")

        inspector = sa.inspect(engine)
        assert "golden_hour_group_announcements" in inspector.get_table_names()
        assert {
            column["name"]
            for column in inspector.get_columns("golden_hour_group_announcements")
        } == set(GoldenHourGroupAnnouncement.__table__.columns.keys())
        assert inspector.get_pk_constraint(
            "golden_hour_group_announcements"
        )["constrained_columns"] == ["group_id", "announcement_day"]
        assert inspector.get_foreign_keys("golden_hour_group_announcements")[0][
            "referred_table"
        ] == "groups"

        with engine.begin() as connection:
            _run(connection, "downgrade")
        assert "golden_hour_group_announcements" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_migration_follows_apple_reward_multiplier():
    migration = _migration()
    assert migration.revision == "069_golden_hour_notices"
    assert migration.down_revision == "068_golden_hour_apple_rewards"
