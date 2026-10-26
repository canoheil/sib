"""Runtime tests for migration 065_league_points.

Only this revision runs here, against a standalone SQLite database that has
the prerequisite ``system_config`` table and the three tables the migration
indexes (created by hand, since the historical chain contains MariaDB-only
DDL).

Pinned here:

* the three covering indexes are created on the right tables/columns — and
  nothing else changes: no table, no column, no row of history;
* ``league_points`` is seeded with the same table ``DEFAULT_POINTS`` holds, so
  code and database can never disagree about the default league;
* ``league_min_points`` is raised 3 -> 15 **only** while it still holds the
  old seeded value: an operator's own number is never overwritten;
* ``upgrade`` is re-runnable and never touches customised config;
* ``downgrade`` drops the indexes, removes only the untouched seeds and puts
  the floor back.
"""

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.services.group_league import DEFAULT_POINTS

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/065_league_points.py"

_INDEXES = {
    "apple_mine_shift": "ix_apple_mine_shift_group_worked_at",
    "apple_duels": "ix_apple_duels_group_created",
    "war_attacks": "ix_war_attacks_created_at",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_065", MIGRATION)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm065.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE apple_mine_shift ("
                "id BIGINT PRIMARY KEY, group_id BIGINT, worked_at DATETIME)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE apple_duels ("
                "id VARCHAR(32) PRIMARY KEY, group_id BIGINT, created_at DATETIME)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE war_attacks ("
                "id BIGINT PRIMARY KEY, damage INT, created_at DATETIME)"
            )
        )
    yield engine
    engine.dispose()


def _indexes(engine, table: str) -> dict[str, list[str]]:
    return {
        index["name"]: list(index["column_names"])
        for index in sa.inspect(engine).get_indexes(table)
    }


def _config(engine) -> dict[str, str]:
    with engine.connect() as connection:
        return dict(
            connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
        )


def _set(engine, key: str, value: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value)"
            ).bindparams(key=key, value=value)
        )


def test_upgrade_creates_the_three_covering_indexes(engine):
    _run(engine, "upgrade")

    assert _indexes(engine, "apple_mine_shift")[
        "ix_apple_mine_shift_group_worked_at"
    ] == ["group_id", "worked_at"]
    assert _indexes(engine, "apple_duels")["ix_apple_duels_group_created"] == [
        "group_id",
        "created_at",
    ]
    assert _indexes(engine, "war_attacks")["ix_war_attacks_created_at"] == [
        "created_at"
    ]


def test_upgrade_adds_no_table_and_no_column(engine):
    with engine.connect() as connection:
        before = {
            name: [column["name"] for column in sa.inspect(connection).get_columns(name)]
            for name in ("apple_mine_shift", "apple_duels", "war_attacks")
        }
        tables_before = set(sa.inspect(connection).get_table_names())
    _run(engine, "upgrade")
    with engine.connect() as connection:
        after = {
            name: [column["name"] for column in sa.inspect(connection).get_columns(name)]
            for name in ("apple_mine_shift", "apple_duels", "war_attacks")
        }
        tables_after = set(sa.inspect(connection).get_table_names())
    assert before == after
    assert tables_before == tables_after


def test_upgrade_seeds_the_table_the_code_uses(engine):
    _run(engine, "upgrade")

    seeded = json.loads(_config(engine)["league_points"])
    assert seeded == DEFAULT_POINTS


def test_the_floor_is_raised_only_while_it_is_still_ours(engine):
    _set(engine, "league_min_points", "3")
    _run(engine, "upgrade")
    assert _config(engine)["league_min_points"] == "15"


def test_a_customised_floor_is_never_overwritten(engine):
    _set(engine, "league_min_points", "40")
    _run(engine, "upgrade")
    assert _config(engine)["league_min_points"] == "40"


def test_a_customised_points_table_is_never_overwritten(engine):
    custom = '{"match_win": 9}'
    _set(engine, "league_points", custom)
    _run(engine, "upgrade")
    assert _config(engine)["league_points"] == custom


def test_upgrade_is_re_runnable(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert _indexes(engine, "apple_duels")["ix_apple_duels_group_created"] == [
        "group_id",
        "created_at",
    ]
    assert json.loads(_config(engine)["league_points"]) == DEFAULT_POINTS


def test_downgrade_removes_only_the_untouched_seed(engine):
    _set(engine, "league_min_points", "3")
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    config = _config(engine)
    assert "league_points" not in config
    assert config["league_min_points"] == "3"
    for table in _INDEXES:
        assert _INDEXES[table] not in _indexes(engine, table)


def test_downgrade_keeps_a_customised_points_table(engine):
    custom = '{"match_win": 9}'
    _set(engine, "league_points", custom)
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    assert _config(engine)["league_points"] == custom


def test_the_migration_revises_the_league_migration(engine):
    migration = _load_migration()
    assert migration.revision == "065_league_points"
    assert migration.down_revision == "064_group_league"
    # The seeded JSON is the same table the service falls back to.
    assert json.loads(migration._LEAGUE_POINTS) == DEFAULT_POINTS
