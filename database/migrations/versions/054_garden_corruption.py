"""Garden corruption: is_withered flag + garden_wither_events ledger.

Revision ID: 054_garden_corruption
Revises: 053_apple_pickaxes
Create Date: 2026-10-20

G2 — Invasion Corruption (پژمردگی) & Divine Fertilizer (کود الهی).

- ``garden_plots.is_withered`` marks a plot corrupted by a lost Invasion.
  It starts at 0 for every existing row, so no data backfill is needed.
- ``garden_wither_events`` is the audit + fairness ledger. The unique
  constraints enforce (a) at most one wither per player per UTC day and
  (b) idempotency when settlement of the same invasion is retried.

The DDL is written portably (batch mode) so the isolated SQLite migration
tests can exercise the same revision as production MariaDB.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "054_garden_corruption"
down_revision: Union[str, None] = "053_apple_pickaxes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("garden_plots") as batch:
        batch.add_column(
            sa.Column(
                "is_withered",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    op.create_table(
        "garden_wither_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("wither_day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["invasion_id"], ["inv2_invasions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "wither_day", name="uq_garden_wither_user_day"),
        sa.UniqueConstraint(
            "user_id", "invasion_id", name="uq_garden_wither_user_invasion"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_garden_wither_user_id", "garden_wither_events", ["user_id"]
    )
    op.create_index(
        "ix_garden_wither_invasion_id", "garden_wither_events", ["invasion_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_garden_wither_invasion_id", table_name="garden_wither_events"
    )
    op.drop_index("ix_garden_wither_user_id", table_name="garden_wither_events")
    op.drop_table("garden_wither_events")
    with op.batch_alter_table("garden_plots") as batch:
        batch.drop_column("is_withered")
