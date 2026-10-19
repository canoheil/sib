"""Direct SQLite test for migration 052 (war attack cap counter).

Runs the revision against a minimal ``war_participants`` table, asserts the
new column is NOT NULL with a portable server default, that existing rows are
backfilled to 0 (so they keep all ten attacks), and that the ORM model and the
migration cannot drift apart silently.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/052_war_attack_count.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_052", MIGRATION
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_participants(engine: sa.engine.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "war_participants",
        metadata,
        sa.Column("war_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("energy", sa.Integer(), nullable=False),
        sa.Column("total_damage", sa.Integer(), nullable=False),
        sa.Column("last_attack_at", sa.DateTime(timezone=True), nullable=True),
    )
    sa.Table(
        "war_attacks",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("war_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
    )
    metadata.create_all(engine)


def _run(engine: sa.engine.Engine, direction: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def test_revision_chain_points_at_051():
    migration = _load_migration()
    assert migration.revision == "052_war_attack_count"
    assert migration.down_revision == "051_referrals"


def test_052_adds_zero_defaulted_attack_count_and_downgrades(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm052.db'}")
    _minimal_participants(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO war_participants "
                "(war_id, user_id, group_id, energy, total_damage) "
                "VALUES (1, 7, -1001, 40, 900), (1, 8, -1001, 100, 0)"
            )
        )
        # User 7 already landed three hits before this column existed.
        connection.execute(
            sa.text(
                "INSERT INTO war_attacks (id, war_id, user_id) "
                "VALUES (1, 1, 7), (2, 1, 7), (3, 1, 7)"
            )
        )

    _run(engine, "upgrade")

    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("war_participants")}
    assert "attack_count" in columns
    assert columns["attack_count"]["nullable"] is False
    assert str(columns["attack_count"]["default"]).strip("'") == "0"

    with engine.connect() as connection:
        # Existing hits are backfilled; a participant without hits keeps all
        # ten attacks available.
        backfilled = dict(
            connection.execute(
                sa.text(
                    "SELECT user_id, attack_count FROM war_participants "
                    "WHERE war_id = 1"
                )
            ).all()
        )
        assert backfilled == {7: 3, 8: 0}

    _run(engine, "downgrade")
    after = {c["name"] for c in sa.inspect(engine).get_columns("war_participants")}
    assert "attack_count" not in after
    engine.dispose()


def test_model_matches_migration():
    from sib.database.models import WarParticipant

    column = WarParticipant.__table__.c.attack_count
    assert column.nullable is False
    assert str(column.server_default.arg) == "0"
    assert column.default.arg == 0
