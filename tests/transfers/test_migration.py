"""Tests for migration 062_transfers.

The historical Alembic chain contains MariaDB-specific DDL, so this focused
test runs ONLY revision 062 against a standalone SQLite database with the two
prerequisite tables (``users``, ``system_config``) created by hand - the same
approach as the other per-revision migration tests (061, 055).
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models import PendingTransfer, TransferLog
from sib.services.config_store import CONFIG_DEFAULTS

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "database/migrations/versions/062_transfers.py"

EXPECTED_CONFIG = {
    "transfer_fee_percent": "0",
    "transfer_min_amount": "1",
    "transfer_max_amount": "10000",
    "transfer_daily_limit": "0",
    "transfer_cooldown_seconds": "0",
    "transfer_enabled": "true",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_062", MIGRATION)
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


@pytest.fixture
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm062.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE users ("
                "id BIGINT PRIMARY KEY, "
                "first_name VARCHAR(255) NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )
    yield engine
    engine.dispose()


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _config(engine) -> dict[str, str]:
    with engine.begin() as connection:
        rows = connection.execute(
            sa.text("SELECT `key`, `value` FROM system_config")
        ).all()
    return {key: value for key, value in rows}


# ---------------------------------------------------------------------------
# Chain position
# ---------------------------------------------------------------------------


def test_migration_062_revises_061_and_is_the_only_head():
    migration = _load_migration()
    assert migration.revision == "062_transfers"
    assert migration.down_revision == "061_seedling_sell_price"

    revisions = {}
    for path in (REPO_ROOT / "database/migrations/versions").glob("*.py"):
        spec = importlib.util.spec_from_file_location(f"rev_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions[module.revision] = module.down_revision

    parents = [parent for parent in revisions.values() if parent]
    assert len(parents) == len(set(parents)), "forked migration history"
    # 062 is no longer the head: 063_reminders (come-back reminders) and then
    # 064_group_league (weekly group league), 065_league_points (its
    # multi-source scoring table), and 066_daily_channel_digest revise it;
    # the history stays linear with exactly one head.
    assert set(revisions) - set(parents) == {"066_daily_channel_digest"}


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def test_upgrade_creates_both_tables_with_their_indexes(engine):
    _run(engine, "upgrade")

    assert {"transfer_log", "pending_transfer"} <= _tables(engine)

    log_indexes = {
        index["name"]
        for index in sa.inspect(engine).get_indexes("transfer_log")
    }
    assert {
        "ix_transfer_log_from_user_id",
        "ix_transfer_log_to_user_id",
        "ix_transfer_log_from_user_created_at",
        "ix_transfer_log_to_user_created_at",
    } <= log_indexes

    pending_indexes = {
        index["name"]
        for index in sa.inspect(engine).get_indexes("pending_transfer")
    }
    assert {
        "ix_pending_transfer_state_expires_at",
        "ix_pending_transfer_from_user_state",
    } <= pending_indexes


def test_tables_match_the_models(engine):
    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    for model in (TransferLog, PendingTransfer):
        migrated = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        assert migrated == set(model.__table__.columns.keys())


def test_upgrade_seeds_the_six_config_keys(engine):
    _run(engine, "upgrade")

    assert _config(engine) == EXPECTED_CONFIG


def test_upgrade_is_idempotent_and_never_overwrites_custom_values(engine):
    _run(engine, "upgrade")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE system_config SET `value` = '7' "
                "WHERE `key` = 'transfer_fee_percent'"
            )
        )

    _run(engine, "upgrade")  # no "table already exists", no duplicate rows

    config = _config(engine)
    assert len(config) == len(EXPECTED_CONFIG)
    assert config["transfer_fee_percent"] == "7"  # customization preserved
    assert config["transfer_max_amount"] == "10000"


def test_config_defaults_match_the_migration_seed():
    migration = _load_migration()
    assert migration._CONFIG_DEFAULTS == EXPECTED_CONFIG

    # CONFIG_DEFAULTS holds the typed fallback of the same values; the DB
    # string decodes to exactly that Python value.
    import json

    for key, seed in EXPECTED_CONFIG.items():
        assert CONFIG_DEFAULTS[key] == json.loads(seed)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def test_downgrade_drops_tables_and_untouched_defaults(engine):
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    assert "transfer_log" not in _tables(engine)
    assert "pending_transfer" not in _tables(engine)
    assert _config(engine) == {}


def test_downgrade_keeps_customized_config_rows(engine):
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE system_config SET `value` = '3' "
                "WHERE `key` = 'transfer_daily_limit'"
            )
        )

    _run(engine, "downgrade")

    assert _config(engine) == {"transfer_daily_limit": "3"}
    assert "transfer_log" not in _tables(engine)
    assert "pending_transfer" not in _tables(engine)
