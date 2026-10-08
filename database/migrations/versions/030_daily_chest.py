"""Add the daily chest claim ledger (gate + reward audit).

Revision ID: 030_daily_chest
Revises: 029_profile_skins
Create Date: 2026-10-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "030_daily_chest"
down_revision: Union[str, None] = "029_profile_skins"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_chest_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_day", sa.String(length=10), nullable=False),
        sa.Column("reward_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "claim_day", name="uq_daily_chest_claim_user_day"),
        sa.CheckConstraint("quantity >= 1", name="ck_daily_chest_claim_quantity"),
    )
    op.create_index("ix_daily_chest_claims_user_id", "daily_chest_claims", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_daily_chest_claims_user_id", table_name="daily_chest_claims")
    op.drop_table("daily_chest_claims")
