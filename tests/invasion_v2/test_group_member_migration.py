"""Migration 037 is isolated, reversible, and never changes existing flags."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select

from sib.database.models import Base, Group, SystemConfig, User


def test_migration_037_up_down_and_config_seed():
    path = Path(__file__).resolve().parents[2] / "database/migrations/versions/037_inv2_group_members.py"
    spec = importlib.util.spec_from_file_location("migration_037", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "036_invasion_v2"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[User.__table__, Group.__table__, SystemConfig.__table__])
        conn.execute(SystemConfig.__table__.insert(), [
            {"key": "invasion_enabled", "value": "false"},
            {"key": "inv2_member_active_window_days", "value": "14"},
        ])
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert "inv2_group_members" in inspect(conn).get_table_names()
        indexes = inspect(conn).get_indexes("inv2_group_members")
        assert {idx["name"] for idx in indexes} == {
            "ix_inv2_group_members_user_id", "ix_inv2_group_members_group_id",
            "ix_inv2_group_members_group_last_seen",
        }
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == "invasion_enabled")) == "false"
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == "inv2_member_active_window_days")) == "14"
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "inv2_group_members" not in inspect(conn).get_table_names()
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == "inv2_member_active_window_days")) == "14"
    engine.dispose()


def test_migration_seeds_missing_window():
    path = Path(__file__).resolve().parents[2] / "database/migrations/versions/037_inv2_group_members.py"
    spec = importlib.util.spec_from_file_location("migration_037_seed", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[User.__table__, Group.__table__, SystemConfig.__table__])
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == "inv2_member_active_window_days")) == "30"
    engine.dispose()
