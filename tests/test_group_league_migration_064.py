"""Runtime tests for migration 064_group_league.

Same approach as the other per-revision migration tests (061, 062, 063): the
historical Alembic chain contains MariaDB-specific DDL, so only revision 064
runs here against a standalone SQLite database with the two prerequisite
tables (``users``, ``system_config``) and the referenced ``groups`` table
created by hand.

Pinned here:

* both tables, their columns, indexes, unique constraint and cascade FKs;
* the four config keys are seeded, including ``league_first_week`` — the guard
  that stops the first deploy from paying a retroactive prize;
* a customised value is never overwritten;
* ``upgrade`` is re-runnable;
* ``downgrade`` removes the tables and only the untouched seeds.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/064_group_league.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_064", MIGRATION)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm064.db'}")
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
                "CREATE TABLE groups ("
                "id BIGINT PRIMARY KEY, "
                "title VARCHAR(255) NOT NULL)"
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


def test_upgrade_creates_the_week_and_award_tables(engine):
    _run(engine, "upgrade")

    assert {"group_league_weeks", "group_league_awards"} <= _tables(engine)

    week_columns = {
        column["name"]
        for column in sa.inspect(engine).get_columns("group_league_weeks")
    }
    assert week_columns == {
        "week_key",
        "finalized_at",
        "winner_group_id",
        "winner_points",
        "apples_paid",
    }
    assert sa.inspect(engine).get_pk_constraint("group_league_weeks")[
        "constrained_columns"
    ] == ["week_key"]

    award_columns = {
        column["name"]
        for column in sa.inspect(engine).get_columns("group_league_awards")
    }
    assert award_columns == {
        "id",
        "week_key",
        "rank",
        "group_id",
        "points",
        "wins",
        "draws",
        "losses",
        "matches",
        "players_paid",
        "apples_per_player",
    }

    uniques = {
        constraint["name"]
        for constraint in sa.inspect(engine).get_unique_constraints(
            "group_league_awards"
        )
    }
    assert "uq_group_league_award_rank" in uniques

    referred = {
        fk["referred_table"]
        for fk in sa.inspect(engine).get_foreign_keys("group_league_awards")
    }
    assert referred == {"group_league_weeks", "groups"}


def test_upgrade_seeds_the_config_defaults(engine):
    _run(engine, "upgrade")

    config = _config(engine)
    assert config["league_enabled"] == "true"
    assert config["league_min_points"] == "3"
    assert '"1": 300' in config["league_rewards"]
    # The first week is THIS week, so no past week is ever paid.
    tehran = ZoneInfo("Asia/Tehran")
    today = datetime.now(tehran).date()
    saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    assert config["league_first_week"] == saturday.isoformat()


def test_upgrade_never_overwrites_a_customised_value(engine):
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('league_enabled', 'false')"
            )
        )

    _run(engine, "upgrade")

    assert _config(engine)["league_enabled"] == "false"


def test_upgrade_is_re_runnable(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert "group_league_weeks" in _tables(engine)
    assert "group_league_awards" in _tables(engine)
    assert _config(engine)["league_min_points"] == "3"


def test_downgrade_drops_the_tables_and_only_untouched_seeds(engine):
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('league_rewards', '{\"1\": 1}')"
            )
        )
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    tables = _tables(engine)
    assert "group_league_weeks" not in tables
    assert "group_league_awards" not in tables
    # The operator's tuned value survives; untouched defaults are removed.
    assert _config(engine) == {"league_rewards": '{"1": 1}'}


def test_the_migration_extends_the_linear_chain():
    migration = _load_migration()
    assert migration.revision == "064_group_league"
    assert migration.down_revision == "063_reminders"
