"""Runtime tests for migration 063_reminders.

The historical Alembic chain contains MariaDB-specific DDL, so this focused
test runs ONLY revision 063 against a standalone SQLite database with the two
prerequisite tables (``users``, ``system_config``) created by hand — the same
approach as the other per-revision migration tests (061, 062).

Pinned here:

* the table, its columns, its indexes and its cascade FK exist;
* the six config keys are seeded and a CUSTOMISED value is never overwritten
  (``alembic upgrade`` must be safe to run on a live, tuned install);
* ``upgrade`` is re-runnable;
* ``downgrade`` removes the rows it seeded and the table, and leaves a
  customised value alone.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/063_reminders.py"

EXPECTED_CONFIG = {
    "reminders_enabled": "true",
    "reminders_min_gap_hours": "20",
    "reminders_window_start_hour": "16",
    "reminders_window_end_hour": "22",
    "reminders_chest_after_hour": "17",
    "reminders_batch_size": "25",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_063", MIGRATION)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm063.db'}")
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


def test_upgrade_creates_the_state_table(engine):
    _run(engine, "upgrade")

    assert "user_reminder_states" in _tables(engine)

    columns = {c["name"] for c in sa.inspect(engine).get_columns("user_reminder_states")}
    assert columns == {
        "user_id",
        "last_sent_at",
        "last_kind",
        "blocked_at",
        "opted_out",
    }

    indexes = {
        index["name"]
        for index in sa.inspect(engine).get_indexes("user_reminder_states")
    }
    assert "ix_user_reminder_states_last_sent_at" in indexes
    assert "ix_user_reminder_states_blocked_at" in indexes

    primary_key = sa.inspect(engine).get_pk_constraint("user_reminder_states")
    assert primary_key["constrained_columns"] == ["user_id"]

    foreign_keys = sa.inspect(engine).get_foreign_keys("user_reminder_states")
    assert foreign_keys[0]["referred_table"] == "users"
    assert foreign_keys[0]["options"].get("ondelete", "").upper() == "CASCADE"


def test_upgrade_seeds_the_config_defaults(engine):
    _run(engine, "upgrade")

    assert _config(engine) == EXPECTED_CONFIG


def test_upgrade_never_overwrites_a_customised_value(engine):
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('reminders_enabled', 'false')"
            )
        )

    _run(engine, "upgrade")

    assert _config(engine)["reminders_enabled"] == "false"


def test_upgrade_is_re_runnable(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert "user_reminder_states" in _tables(engine)
    assert _config(engine) == EXPECTED_CONFIG


def test_downgrade_drops_the_table_and_only_untouched_seeds(engine):
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('reminders_batch_size', '100')"
            )
        )
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    assert "user_reminder_states" not in _tables(engine)
    # The operator's tuned value survives; the untouched defaults are gone.
    assert _config(engine) == {"reminders_batch_size": "100"}
