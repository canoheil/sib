"""Persist auditable Invasion defense-action results.

Revision ID: 028_invasion_defense_actions
Revises: 027_invasion_contrib_keys
Create Date: 2026-10-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028_invasion_defense_actions"
down_revision: Union[str, None] = "027_invasion_contrib_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "invasion_defense_actions",
        sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("participant_id", sa.BigInteger(), nullable=False),
        sa.Column("wave_id", sa.BigInteger(), nullable=False),
        sa.Column("contribution_id", sa.BigInteger(), nullable=False),
        sa.Column("wave_sequence", sa.Integer(), nullable=False),
        sa.Column("action_code", sa.String(length=32), nullable=False),
        sa.Column(
            "action_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("power_snapshot_used", sa.Integer(), nullable=True),
        sa.Column("contribution_amount", sa.BigInteger(), nullable=False),
        sa.Column("participant_total_after", sa.BigInteger(), nullable=False),
        sa.Column("wave_total_after", sa.BigInteger(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["invasion_id"], ["invasions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["invasion_participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["wave_id"], ["invasion_waves.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["contribution_id"], ["invasion_contributions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "invasion_id",
            "idempotency_key",
            name="uq_invasion_defense_actions_invasion_key",
        ),
        sa.UniqueConstraint(
            "contribution_id",
            name="uq_invasion_defense_actions_contribution",
        ),
        sa.CheckConstraint(
            "wave_sequence > 0",
            name="ck_invasion_defense_actions_wave_sequence",
        ),
        sa.CheckConstraint(
            "action_version > 0",
            name="ck_invasion_defense_actions_version",
        ),
        sa.CheckConstraint(
            "contribution_amount > 0",
            name="ck_invasion_defense_actions_positive_contribution",
        ),
        sa.CheckConstraint(
            "power_snapshot_used IS NULL OR power_snapshot_used >= 0",
            name="ck_invasion_defense_actions_power_snapshot",
        ),
        sa.CheckConstraint(
            "participant_total_after >= 0 AND wave_total_after >= 0",
            name="ck_invasion_defense_actions_totals",
        ),
    )
    op.create_index(
        "ix_invasion_defense_actions_participant_created",
        "invasion_defense_actions",
        ["participant_id", "created_at"],
    )
    op.create_index(
        "ix_invasion_defense_actions_wave_created",
        "invasion_defense_actions",
        ["wave_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invasion_defense_actions_wave_created",
        table_name="invasion_defense_actions",
    )
    op.drop_index(
        "ix_invasion_defense_actions_participant_created",
        table_name="invasion_defense_actions",
    )
    op.drop_table("invasion_defense_actions")
