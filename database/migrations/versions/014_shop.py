"""Add weekly rotating shop.

Revision ID: 014_shop
Revises: 013_player_cards
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "014_shop"
down_revision: Union[str, None] = "013_player_cards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shop_rotations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("week_id", sa.String(length=32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("week_id", name="uq_shop_rotations_week_id"),
    )

    op.create_table(
        "shop_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rotation_id", sa.BigInteger(), nullable=False),
        sa.Column("character_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_type", sa.String(length=20), nullable=False, server_default="CHARACTER"),
        sa.Column("price_override", sa.Integer(), nullable=True),
        sa.Column("badge", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rotation_id", "character_id", name="uq_shop_entry_rotation_character"),
    )
    op.create_index("ix_shop_entries_rotation_id", "shop_entries", ["rotation_id"])
    op.create_index("ix_shop_entries_character_id", "shop_entries", ["character_id"])


def downgrade() -> None:
    op.drop_index("ix_shop_entries_character_id", table_name="shop_entries")
    op.drop_index("ix_shop_entries_rotation_id", table_name="shop_entries")
    op.drop_table("shop_entries")
    op.drop_table("shop_rotations")
