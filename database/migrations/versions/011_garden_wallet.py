"""Add garden, apple wallet/ledger, and system config.

Revision ID: 011_garden_wallet
Revises: 010_seedlings
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "011_garden_wallet"
down_revision: Union[str, None] = "010_seedlings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "gardens",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("capacity_plots", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "garden_plots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("garden_id", sa.BigInteger(), nullable=False),
        sa.Column("plot_index", sa.Integer(), nullable=False),
        sa.Column("planted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mature_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fruit_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cycle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["garden_id"], ["gardens.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("garden_id", "plot_index", name="uq_garden_plot_index"),
    )
    op.create_index("ix_garden_plots_garden_id", "garden_plots", ["garden_id"])

    op.create_table(
        "apple_wallets",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "apple_transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column("ref_type", sa.String(length=50), nullable=True),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_apple_tx_idempotency"),
    )
    op.create_index("ix_apple_transactions_user_id", "apple_transactions", ["user_id"])
    op.create_index("ix_apple_transactions_reason", "apple_transactions", ["reason"])


def downgrade() -> None:
    op.drop_index("ix_apple_transactions_reason", table_name="apple_transactions")
    op.drop_index("ix_apple_transactions_user_id", table_name="apple_transactions")
    op.drop_table("apple_transactions")
    op.drop_table("apple_wallets")
    op.drop_index("ix_garden_plots_garden_id", table_name="garden_plots")
    op.drop_table("garden_plots")
    op.drop_table("gardens")
    op.drop_table("system_config")
