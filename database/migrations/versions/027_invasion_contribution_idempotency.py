"""Persist idempotent Invasion contribution requests.

Revision ID: 027_invasion_contrib_keys
Revises: 026_invasion_persistence
Create Date: 2026-10-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "027_invasion_contrib_keys"
down_revision: Union[str, None] = "026_invasion_persistence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "invasion_contributions",
        sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("participant_id", sa.BigInteger(), nullable=False),
        sa.Column("wave_id", sa.BigInteger(), nullable=False),
        sa.Column("wave_sequence", sa.Integer(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
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
        sa.UniqueConstraint(
            "invasion_id",
            "idempotency_key",
            name="uq_invasion_contributions_invasion_key",
        ),
        sa.CheckConstraint(
            "amount > 0", name="ck_invasion_contributions_positive_amount"
        ),
        sa.CheckConstraint(
            "wave_sequence > 0", name="ck_invasion_contributions_wave_sequence"
        ),
    )


def downgrade() -> None:
    op.drop_table("invasion_contributions")
