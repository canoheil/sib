"""Direct SQLite tests for migration 031 (four-wave Invasion battles).

The full historical chain contains MariaDB-only DDL, so, as with the other
portable migrations, this revision is executed against a minimal schema that
has exactly the columns it touches.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/031_invasion_four_wave_battles.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_031", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_schema(engine: sa.engine.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "invasions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    sa.Table(
        "invasion_defense_actions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("participant_id", sa.Integer, nullable=False),
        sa.Column("wave_id", sa.Integer, nullable=False),
        sa.Column("action_code", sa.String(32), nullable=False),
    )
    metadata.create_all(engine)


def _run(engine: sa.engine.Engine, direction: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def test_031_adds_columns_backfills_history_and_enforces_one_action_per_wave(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm031.db'}")
    _minimal_schema(engine)
    finished_at = datetime(2026, 10, 8, 12, 20, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO invasions (id, status, ended_at) VALUES "
                "(1, 'FINISHED', :ended), (2, 'ACTIVE', NULL), (3, 'FINISHED', NULL)"
            ),
            {"ended": finished_at},
        )
        connection.execute(
            sa.text(
                "INSERT INTO invasion_defense_actions (id, participant_id, wave_id, action_code) "
                "VALUES (1, 10, 100, 'brace'), (2, 10, 101, 'brace')"
            )
        )

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("invasions")}
    assert {"heartbeat_at", "rewards_settled_at", "presentation_stage"} <= columns
    indexes = {index["name"] for index in inspector.get_indexes("invasions")}
    assert "ix_invasions_status_rewards_settled_at" in indexes

    with engine.connect() as connection:
        settled = dict(
            connection.execute(
                sa.text("SELECT id, rewards_settled_at FROM invasions")
            ).all()
        )
    # Historical finished events are not retro-paid; unfinished ones stay pending.
    assert settled[1] is not None
    assert settled[2] is None
    assert settled[3] is not None  # FINISHED without ended_at still gets a marker

    # Distinct waves for the same participant are allowed.
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO invasion_defense_actions (id, participant_id, wave_id, action_code) "
                "VALUES (3, 11, 100, 'brace')"
            )
        )
    # A second brace by the same participant in the same wave is rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO invasion_defense_actions (id, participant_id, wave_id, action_code) "
                    "VALUES (4, 10, 100, 'brace')"
                )
            )

    _run(engine, "downgrade")
    after = {column["name"] for column in sa.inspect(engine).get_columns("invasions")}
    assert not ({"heartbeat_at", "rewards_settled_at", "presentation_stage"} & after)
    with engine.begin() as connection:
        # The constraint is gone again after downgrade.
        connection.execute(
            sa.text(
                "INSERT INTO invasion_defense_actions (id, participant_id, wave_id, action_code) "
                "VALUES (4, 10, 100, 'brace')"
            )
        )
    engine.dispose()
