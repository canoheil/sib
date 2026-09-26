"""Add apple battles + battle queue, extend seedling ledger for battle awards.

Revision ID: 016_apple_battles
Revises: 015_trades
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "016_apple_battles"
down_revision: Union[str, None] = "015_trades"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "apple_battles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("player1_id", sa.BigInteger(), nullable=False),
        sa.Column("player2_id", sa.BigInteger(), nullable=False),
        sa.Column("player1_card_id", sa.BigInteger(), nullable=False),
        sa.Column("player2_card_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("winner_id", sa.BigInteger(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("result_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("balance_version", sa.Integer(), nullable=True),
        sa.Column("rng_seed", sa.String(length=64), nullable=True),
        sa.Column("seedlings_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_apple_battles_player1_id", "apple_battles", ["player1_id"])
    op.create_index("ix_apple_battles_player2_id", "apple_battles", ["player2_id"])
    op.create_index("ix_apple_battles_status", "apple_battles", ["status"])

    op.create_table(
        "apple_battle_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("card_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_apple_battle_queue_user"),
    )
    op.create_index("ix_apple_battle_queue_user_id", "apple_battle_queue", ["user_id"])
    op.create_index("ix_apple_battle_queue_level", "apple_battle_queue", ["level"])

    # Seedling ledger: source of the award (match vs battle).
    op.add_column("seedling_awards", sa.Column("source", sa.String(length=20), nullable=False, server_default="match"))
    op.add_column("seedling_awards", sa.Column("source_ref_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(
        "uq_seedling_award_source_ref_user",
        "seedling_awards",
        ["source", "source_ref_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_seedling_award_source_ref_user", "seedling_awards", type_="unique")
    op.drop_column("seedling_awards", "source_ref_id")
    op.drop_column("seedling_awards", "source")
    op.drop_index("ix_apple_battle_queue_level", table_name="apple_battle_queue")
    op.drop_index("ix_apple_battle_queue_user_id", table_name="apple_battle_queue")
    op.drop_table("apple_battle_queue")
    op.drop_index("ix_apple_battles_status", table_name="apple_battles")
    op.drop_index("ix_apple_battles_player2_id", table_name="apple_battles")
    op.drop_index("ix_apple_battles_player1_id", table_name="apple_battles")
    op.drop_table("apple_battles")
