"""Add bot assets registry and generic user item inventory.

Additive-only migration: two NEW tables. No existing table, column or
data is modified.

Revision ID: 017_assets_items
Revises: 016_apple_battles
Create Date: 2026-09-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "017_assets_items"
down_revision: Union[str, None] = "016_apple_battles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Central asset registry: features reference assets by KEY, never by
    # raw telegram file_id. Replacing artwork = one row update.
    op.create_table(
        "bot_assets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False, server_default="photo"),
        sa.Column("file_id", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_bot_assets_key"),
    )

    # Generic item inventory (harvest drops today; trading/shop/crafting
    # later). One row per (user, item_key) with a quantity.
    op.create_table(
        "user_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("item_key", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "item_key", name="uq_user_items_user_key"),
    )
    op.create_index("ix_user_items_user_id", "user_items", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_items_user_id", table_name="user_items")
    op.drop_table("user_items")
    op.drop_table("bot_assets")
