"""Direct SQLite test for migration 054 (garden corruption).

The full historical chain contains MariaDB-only DDL, so — as with the other
portable migration tests — this revision is executed against a minimal schema
containing exactly the tables and columns it touches.
"""

import importlib.util
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "database/migrations/versions/054_garden_corruption.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_054", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_schema(engine: sa.engine.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(
        "inv2_invasions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    gardens = sa.Table(
        "gardens",
        metadata,
        sa.Column("user_id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "garden_plots",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("garden_id", sa.Integer, sa.ForeignKey(gardens.c.user_id)),
        sa.Column("plot_index", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)


def _run(engine: sa.engine.Engine, direction: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def test_054_adds_wither_flag_and_ledger_with_fairness_constraints(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm054.db'}")
    _minimal_schema(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id) VALUES (7)"))
        conn.execute(sa.text("INSERT INTO inv2_invasions (id) VALUES (11)"))
        conn.execute(sa.text("INSERT INTO inv2_invasions (id) VALUES (12)"))
        conn.execute(sa.text("INSERT INTO gardens (user_id) VALUES (7)"))
        conn.execute(
            sa.text("INSERT INTO garden_plots (garden_id, plot_index) VALUES (7, 0)")
        )

    _run(engine, "upgrade")

    with engine.begin() as conn:
        # Existing plots are backfilled to healthy (0).
        default_value = conn.execute(
            sa.text("SELECT is_withered FROM garden_plots WHERE garden_id = 7")
        ).scalar_one()
        assert default_value == 0
        # The ledger exists and stores a wither day as a real DATE.
        conn.execute(
            sa.text(
                "INSERT INTO garden_wither_events "
                "(id, user_id, invasion_id, wither_day, created_at, updated_at) "
                "VALUES (1, 7, 11, :day, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"day": date(2026, 10, 20)},
        )

    # Once per UTC day: a second event for the same user/day is rejected.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO garden_wither_events "
                    "(id, user_id, invasion_id, wither_day, created_at, updated_at) "
                    "VALUES (2, 7, 12, :day, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"day": date(2026, 10, 20)},
            )

    # Idempotency: the same invasion cannot wither the same user twice,
    # even on another day (settlement retry protection).
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO garden_wither_events "
                    "(id, user_id, invasion_id, wither_day, created_at, updated_at) "
                    "VALUES (3, 7, 11, :day, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"day": date(2026, 10, 21)},
            )

    # A different day with a different invasion is allowed.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO garden_wither_events "
                "(id, user_id, invasion_id, wither_day, created_at, updated_at) "
                "VALUES (4, 7, 12, :day, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"day": date(2026, 10, 21)},
        )

    _run(engine, "downgrade")
    inspector = sa.inspect(engine)
    assert "garden_wither_events" not in inspector.get_table_names()
    plot_columns = {col["name"] for col in inspector.get_columns("garden_plots")}
    assert "is_withered" not in plot_columns
    engine.dispose()


def test_054_revision_chain():
    migration = _load_migration()
    assert migration.revision == "054_garden_corruption"
    assert migration.down_revision == "053_apple_pickaxes"
