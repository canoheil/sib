"""Tests for database migrations.

The complete historical Alembic ancestry includes MariaDB-specific DDL that
SQLite cannot execute. The chain-head test therefore uses a standalone stamped
database; a focused test below stamps a minimal 024 baseline and runs Alembic
through revisions 025-029 on SQLite to verify progression, Invasion, and
profile-skin schema changes, constraints, indexes, foreign keys, and supported
downgrades. This does not replace a live MySQL/MariaDB migration run.
Portable data migrations are tested directly too.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from datetime import datetime
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


def test_migrations_025_through_029_and_downgrade_on_sqlite():
    """Run the Alembic path from a minimal 024 baseline and inspect its schema."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'invasion_migrations.db'}"
        engine = sa.create_engine(db_url)
        metadata = sa.MetaData()
        users = sa.Table(
            "users",
            metadata,
            sa.Column("id", sa.BigInteger(), primary_key=True),
        )
        groups = sa.Table(
            "groups",
            metadata,
            sa.Column("id", sa.BigInteger(), primary_key=True),
        )
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(users.insert(), [{"id": 7}])
        engine.dispose()

        # The full pre-025 database ancestry has MariaDB-only DDL. Stamp only
        # that baseline, then let Alembic itself execute every target revision.
        stamp = _run_alembic("stamp", "024_marriage_and_love", db_url=db_url)
        assert stamp.returncode == 0, f"{stamp.stdout}\n{stamp.stderr}"
        upgrade = _run_alembic(
            "upgrade", "029_profile_skins", db_url=db_url
        )
        assert upgrade.returncode == 0, f"{upgrade.stdout}\n{upgrade.stderr}"
        current = _run_alembic("current", db_url=db_url)
        assert current.returncode == 0
        assert "029_profile_skins" in current.stdout

        engine = sa.create_engine(db_url)
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            assert {
                "player_progressions",
                "player_xp_grants",
                "invasions",
                "invasion_waves",
                "invasion_participants",
                "invasion_contributions",
                "invasion_defense_actions",
            } <= tables
            progressions = sa.Table(
                "player_progressions", sa.MetaData(), autoload_with=connection
            )
            assert connection.execute(
                sa.select(progressions.c.user_id, progressions.c.total_xp)
            ).all() == [(7, 0)]
            users_table = sa.Table("users", sa.MetaData(), autoload_with=connection)
            assert users_table.c.profile_skin.nullable is False
            assert connection.execute(
                sa.select(users_table.c.profile_skin).where(users_table.c.id == 7)
            ).scalar_one() == "green_apple"

            def unique_names(table_name):
                return {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints(table_name)
                }

            def check_names(table_name):
                return {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints(table_name)
                }

            def index_names(table_name):
                return {index["name"] for index in inspector.get_indexes(table_name)}

            def foreign_keys(table_name):
                return inspector.get_foreign_keys(table_name)

            assert "uq_invasions_occurrence_key" in unique_names("invasions")
            assert {
                "ck_invasions_status",
                "ck_invasions_outcome",
            } <= check_names("invasions")
            assert {
                "ix_invasions_group_status",
                "ix_invasions_status_next_transition_at",
            } <= index_names("invasions")
            assert {
                (fk["referred_table"], fk["options"].get("ondelete"))
                for fk in foreign_keys("invasions")
            } == {("groups", "CASCADE")}

            assert "uq_invasion_waves_invasion_sequence" in unique_names(
                "invasion_waves"
            )
            assert "ck_invasion_waves_status" in check_names("invasion_waves")
            assert "ix_invasion_waves_status_next_transition_at" in index_names(
                "invasion_waves"
            )
            assert {
                (fk["referred_table"], fk["options"].get("ondelete"))
                for fk in foreign_keys("invasion_waves")
            } == {("invasions", "CASCADE")}

            assert "uq_invasion_participants_invasion_user" in unique_names(
                "invasion_participants"
            )
            assert (
                "ck_invasion_participants_main_card_snapshot_complete"
                in check_names("invasion_participants")
            )
            assert {
                (fk["referred_table"], fk["options"].get("ondelete"))
                for fk in foreign_keys("invasion_participants")
            } == {("invasions", "CASCADE"), ("users", "CASCADE")}

            assert "uq_invasion_contributions_invasion_key" in unique_names(
                "invasion_contributions"
            )
            assert {
                "ck_invasion_contributions_positive_amount",
                "ck_invasion_contributions_wave_sequence",
            } <= check_names("invasion_contributions")
            assert {
                (fk["referred_table"], fk["options"].get("ondelete"))
                for fk in foreign_keys("invasion_contributions")
            } == {
                ("invasions", "CASCADE"),
                ("invasion_participants", "CASCADE"),
                ("invasion_waves", "CASCADE"),
            }

            assert {
                "uq_invasion_defense_actions_invasion_key",
                "uq_invasion_defense_actions_contribution",
            } <= unique_names("invasion_defense_actions")
            assert {
                "ck_invasion_defense_actions_wave_sequence",
                "ck_invasion_defense_actions_version",
                "ck_invasion_defense_actions_positive_contribution",
                "ck_invasion_defense_actions_power_snapshot",
                "ck_invasion_defense_actions_totals",
            } <= check_names("invasion_defense_actions")
            assert {
                "ix_invasion_defense_actions_participant_created",
                "ix_invasion_defense_actions_wave_created",
            } <= index_names("invasion_defense_actions")
            assert {
                (fk["referred_table"], fk["options"].get("ondelete"))
                for fk in foreign_keys("invasion_defense_actions")
            } == {
                ("invasions", "CASCADE"),
                ("invasion_participants", "CASCADE"),
                ("invasion_waves", "CASCADE"),
                ("invasion_contributions", "CASCADE"),
            }
        engine.dispose()

        downgrade = _run_alembic(
            "downgrade", "024_marriage_and_love", db_url=db_url
        )
        assert downgrade.returncode == 0, f"{downgrade.stdout}\n{downgrade.stderr}"
        engine = sa.create_engine(db_url)
        with engine.connect() as connection:
            remaining = set(sa.inspect(connection).get_table_names())
            assert {"groups", "users"} <= remaining
            assert "profile_skin" not in {
                column["name"] for column in sa.inspect(connection).get_columns("users")
            }
            assert not {
                "player_progressions",
                "player_xp_grants",
                "invasions",
                "invasion_waves",
                "invasion_participants",
                "invasion_contributions",
                "invasion_defense_actions",
            } & remaining
        engine.dispose()


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


def test_player_xp_migration_starts_accounts_at_zero_and_marks_legacy_milestones():
    """Rollout adds zero-XP rows and idempotency markers without backfill."""
    from sib.services.config_store import CONFIG_DEFAULTS

    # The approved migration's plot-index boundary matches Sib's shipped
    # default (indices 0..4 free; index 5 is the first paid unlock).
    assert CONFIG_DEFAULTS["garden_initial_plots"] == 5
    path = REPO_ROOT / "database/migrations/versions/025_player_xp_progression.py"
    spec = importlib.util.spec_from_file_location("migration_025", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True),
    )
    player_cards = sa.Table(
        "player_cards",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("character_id", sa.BigInteger, nullable=False),
    )
    matches = sa.Table(
        "matches",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("result_applied", sa.Boolean, nullable=False),
    )
    marriages = sa.Table(
        "marriages",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user1_id", sa.BigInteger, nullable=False),
        sa.Column("user2_id", sa.BigInteger, nullable=False),
    )
    apple_transactions = sa.Table(
        "apple_transactions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
    )
    garden_plots = sa.Table(
        "garden_plots",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("garden_id", sa.BigInteger, nullable=False),
        sa.Column("plot_index", sa.Integer, nullable=False),
        sa.Column("planted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("magic_level", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(users.insert(), [{"id": 1}, {"id": 2}])
        connection.execute(
            matches.insert(),
            [{"id": 1, "result_applied": True}, {"id": 2, "result_applied": False}],
        )
        connection.execute(
            player_cards.insert(),
            [
                {"id": 1, "user_id": 1, "character_id": 101},
                {"id": 2, "user_id": 2, "character_id": 102},
            ],
        )
        connection.execute(
            marriages.insert(),
            [{"id": 1, "user1_id": 1, "user2_id": 2}],
        )
        connection.execute(
            apple_transactions.insert(),
            [
                {
                    "id": 1,
                    "user_id": 1,
                    "reason": "garden_plot_unlock",
                    "idempotency_key": "garden_unlock:1:5",
                }
            ],
        )
        connection.execute(
            garden_plots.insert(),
            [
                {
                    "id": 1,
                    "garden_id": 1,
                    "plot_index": 0,
                    "planted_at": datetime(2026, 1, 1),
                    "magic_level": 1,
                },
                {
                    "id": 2,
                    "garden_id": 1,
                    "plot_index": 5,
                    "planted_at": None,
                    "magic_level": 0,
                },
                {
                    "id": 3,
                    "garden_id": 1,
                    "plot_index": 4,
                    "planted_at": None,
                    "magic_level": 0,
                },
            ],
        )

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        migrated_matches = sa.Table("matches", sa.MetaData(), autoload_with=connection)
        match_xp_states = connection.execute(
            sa.select(migrated_matches.c.xp_awarded).order_by(migrated_matches.c.id)
        ).scalars().all()
        assert match_xp_states == [True, False]

        progression = sa.Table(
            "player_progressions", sa.MetaData(), autoload_with=connection
        )
        rows = connection.execute(sa.select(progression).order_by(progression.c.user_id)).all()
        assert [(row.user_id, row.total_xp) for row in rows] == [(1, 0), (2, 0)]

        grants = sa.Table("player_xp_grants", sa.MetaData(), autoload_with=connection)
        markers = connection.execute(
            sa.select(grants.c.user_id, grants.c.source, grants.c.event_key, grants.c.amount)
        ).all()
        marker_set = {(row.user_id, row.source, row.event_key, row.amount) for row in markers}

    assert marker_set == {
        (1, "card_collection", "card_collection:101", 0),
        (2, "card_collection", "card_collection:102", 0),
        (1, "first_marriage", "first_marriage", 0),
        (2, "first_marriage", "first_marriage", 0),
        (1, "garden_plant", "garden_plant:1:0", 0),
        (1, "garden_unlock", "garden_unlock:1:5", 0),
        (1, "garden_magic", "garden_magic:1:0:1", 0),
    }
    assert all(amount == 0 for _uid, _source, _key, amount in marker_set)
