"""Add seedling inventory and award ledger.

Revision ID: 010_seedlings
Revises: 009_remove_elo
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "010_seedlings"
down_revision: Union[str, None] = "009_remove_elo"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "seedling_inventories",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "seedling_awards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(length=50), nullable=False, server_default="match_win"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "user_id", name="uq_seedling_award_match_user"),
    )
    op.create_index("ix_seedling_awards_match_id", "seedling_awards", ["match_id"])
    op.create_index("ix_seedling_awards_user_id", "seedling_awards", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_seedling_awards_user_id", table_name="seedling_awards")
    op.drop_index("ix_seedling_awards_match_id", table_name="seedling_awards")
    op.drop_table("seedling_awards")
    op.drop_table("seedling_inventories")
