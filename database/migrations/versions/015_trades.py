"""Add card trading.

Revision ID: 015_trades
Revises: 014_shop
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "015_trades"
down_revision: Union[str, None] = "014_shop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proposer_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("proposer_card_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_card_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("proposer_confirmed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("recipient_confirmed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trades_proposer_id", "trades", ["proposer_id"])
    op.create_index("ix_trades_recipient_id", "trades", ["recipient_id"])
    op.create_index("ix_trades_status", "trades", ["status"])


def downgrade() -> None:
    op.drop_index("ix_trades_status", table_name="trades")
    op.drop_index("ix_trades_recipient_id", table_name="trades")
    op.drop_index("ix_trades_proposer_id", table_name="trades")
    op.drop_table("trades")
