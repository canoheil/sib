"""Runtime tests for migration 066_daily_channel_digest."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models.daily_channel_post import DailyChannelPost
from sib.services.config_store import CONFIG_DEFAULTS

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/066_daily_channel_digest.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_066", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, direction: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def _config(engine) -> dict[str, str]:
    with engine.connect() as connection:
        return dict(
            connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
        )


def _engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm066.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )
    return engine


def test_upgrade_creates_table_matching_the_model_and_seeds_defaults(tmp_path):
    engine = _engine(tmp_path)
    try:
        _run(engine, "upgrade")
        inspector = sa.inspect(engine)

        assert "daily_channel_posts" in inspector.get_table_names()
        migrated_columns = {
            column["name"] for column in inspector.get_columns("daily_channel_posts")
        }
        assert migrated_columns == set(DailyChannelPost.__table__.columns.keys())
        assert inspector.get_pk_constraint("daily_channel_posts")["constrained_columns"] == [
            "post_day"
        ]
        config = _config(engine)
        assert config["daily_digest_enabled"] == "true"
        assert config["daily_digest_hour"] == "20"
        assert CONFIG_DEFAULTS["daily_digest_enabled"] is False
        assert CONFIG_DEFAULTS["daily_digest_hour"] == 20
        assert CONFIG_DEFAULTS["daily_digest_channel_id"] == ""
    finally:
        engine.dispose()


def test_upgrade_is_rerunnable_and_preserves_custom_settings(tmp_path):
    engine = _engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO system_config (`key`, `value`) VALUES "
                    "('daily_digest_enabled', 'false'), "
                    "('daily_digest_hour', '21')"
                )
            )
        _run(engine, "upgrade")
        _run(engine, "upgrade")

        config = _config(engine)
        assert config["daily_digest_enabled"] == "false"
        assert config["daily_digest_hour"] == "21"
    finally:
        engine.dispose()


def test_downgrade_drops_delivery_log_and_only_untouched_defaults(tmp_path):
    engine = _engine(tmp_path)
    try:
        _run(engine, "upgrade")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE system_config SET `value` = 'false' "
                    "WHERE `key` = 'daily_digest_enabled'"
                )
            )
        _run(engine, "downgrade")

        assert "daily_channel_posts" not in sa.inspect(engine).get_table_names()
        assert _config(engine) == {"daily_digest_enabled": "false"}
    finally:
        engine.dispose()


def test_migration_is_the_single_head_after_league_points():
    migration = _load_migration()
    assert migration.revision == "066_daily_channel_digest"
    assert migration.down_revision == "065_league_points"
    assert migration._CONFIG_DEFAULTS == {
        "daily_digest_enabled": "true",
        "daily_digest_hour": "20",
    }
