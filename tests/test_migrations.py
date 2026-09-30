"""Tests for database migrations.

The migration DDL itself targets MariaDB (ALTER TABLE constraints),
which SQLite cannot execute; the chain is therefore validated against a
standalone stamped database instead of a live `upgrade head` run. The
bot-result data migration is tested directly against SQLite because its
portable SQL should work on both supported test and production dialects.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(*args: str, db_url: str) -> subprocess.CompletedProcess:
    """Run an alembic command against ``db_url`` (never the real database)."""
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = db_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_migration_chain_has_single_head():
    """There must be exactly one head revision (no forked history)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'heads_check.db'}"
        result = _run_alembic("heads", db_url=db_url)
    assert result.returncode == 0
    heads = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1
    assert "(head)" in heads[0]


def test_alembic_current_revision():
    """A freshly stamped database reports the chain's head revision."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'current_check.db'}"
        stamp = _run_alembic("stamp", "head", db_url=db_url)
        assert stamp.returncode == 0

        current = _run_alembic("current", db_url=db_url)
    assert current.returncode == 0
    assert "head" in current.stdout


def test_bot_match_migration_backfills_historical_counters_portably():
    """Legacy finalized bot outcomes leave PvP counters and remain auditable."""
    path = REPO_ROOT / "database/migrations/versions/021_bot_fallback_matchmaking.py"
    spec = importlib.util.spec_from_file_location("migration_021", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    users = sa.Table(
        "users", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("is_bot", sa.Boolean, nullable=False),
    )
    matches = sa.Table(
        "matches", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("is_bot_match", sa.Boolean, nullable=False),
        sa.Column("result_applied", sa.Boolean, nullable=False),
    )
    match_players = sa.Table(
        "match_players", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("match_id", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("result", sa.String),
    )
    sa.Table("matchmaking_queue", metadata, sa.Column("id", sa.Integer, primary_key=True))
    player_stats = sa.Table(
        "player_stats", metadata,
        sa.Column("user_id", sa.Integer, primary_key=True),
        sa.Column("matches_played", sa.Integer, nullable=False),
        sa.Column("wins", sa.Integer, nullable=False),
        sa.Column("losses", sa.Integer, nullable=False),
        sa.Column("draws", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(users.insert(), [{"id": 1, "is_bot": False}, {"id": 2, "is_bot": True}])
        connection.execute(
            matches.insert(),
            [
                {"id": 1, "is_bot_match": True, "result_applied": True},
                {"id": 2, "is_bot_match": True, "result_applied": True},
                {"id": 3, "is_bot_match": True, "result_applied": False},
                {"id": 4, "is_bot_match": False, "result_applied": True},
                {"id": 5, "is_bot_match": False, "result_applied": True},
            ],
        )
        connection.execute(
            match_players.insert(),
            [
                {"id": 1, "match_id": 1, "user_id": 1, "result": "WIN"},
                {"id": 2, "match_id": 2, "user_id": 1, "result": "LOSS"},
                {"id": 3, "match_id": 3, "user_id": 1, "result": "WIN"},
                {"id": 4, "match_id": 1, "user_id": 2, "result": "LOSS"},
                {"id": 5, "match_id": 4, "user_id": 1, "result": "WIN"},
                {"id": 6, "match_id": 5, "user_id": 1, "result": "WIN"},
            ],
        )
        connection.execute(
            player_stats.insert(),
            [
                {"user_id": 1, "matches_played": 4, "wins": 3, "losses": 1, "draws": 0},
                {"user_id": 2, "matches_played": 1, "wins": 0, "losses": 1, "draws": 0},
            ],
        )

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        migrated_stats = sa.Table("player_stats", sa.MetaData(), autoload_with=connection)
        human = connection.execute(
            sa.select(migrated_stats).where(migrated_stats.c.user_id == 1)
        ).mappings().one()
        bot = connection.execute(
            sa.select(migrated_stats).where(migrated_stats.c.user_id == 2)
        ).mappings().one()

    assert (human["matches_played"], human["wins"], human["losses"], human["draws"]) == (2, 2, 0, 0)
    assert (human["bot_matches_played"], human["bot_wins"], human["bot_losses"], human["bot_draws"]) == (2, 1, 1, 0)
    # Internal bot entities keep their legacy counters; they are not shown as players.
    assert (bot["matches_played"], bot["wins"], bot["losses"]) == (1, 0, 1)
    assert bot["bot_matches_played"] == 0

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        restored_stats = sa.Table("player_stats", sa.MetaData(), autoload_with=connection)
        human = connection.execute(
            sa.select(restored_stats).where(restored_stats.c.user_id == 1)
        ).mappings().one()
        bot = connection.execute(
            sa.select(restored_stats).where(restored_stats.c.user_id == 2)
        ).mappings().one()
    assert (human["matches_played"], human["wins"], human["losses"], human["draws"]) == (4, 3, 1, 0)
    assert (bot["matches_played"], bot["wins"], bot["losses"]) == (1, 0, 1)
