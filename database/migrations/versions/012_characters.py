"""Add characters and balance version snapshots.

Revision ID: 012_characters
Revises: 011_garden_wallet
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "012_characters"
down_revision: Union[str, None] = "011_garden_wallet"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "characters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name_fa", sa.String(length=120), nullable=False),
        sa.Column("name_en", sa.String(length=120), nullable=True),
        sa.Column("lore_fa", sa.Text(), nullable=False, server_default=""),
        sa.Column("artwork", sa.String(length=255), nullable=True),
        sa.Column("power", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("speed", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("intelligence", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("passive_key", sa.String(length=64), nullable=True),
        sa.Column("passive_data", sa.Text(), nullable=True),
        sa.Column("hidden_modifiers", sa.Text(), nullable=True),
        sa.Column("strengths", sa.Text(), nullable=True),
        sa.Column("weaknesses", sa.Text(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("rarity", sa.String(length=20), nullable=False, server_default="COMMON"),
        sa.Column("release_status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("shop_enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("balance_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_characters_code"),
    )
    op.create_index("ix_characters_code", "characters", ["code"])
    op.create_index("ix_characters_release_status", "characters", ["release_status"])

    op.create_table(
        "character_balance_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("character_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("character_id", "version", name="uq_character_balance_version"),
    )
    op.create_index("ix_character_balance_versions_character_id", "character_balance_versions", ["character_id"])


def downgrade() -> None:
    op.drop_index("ix_character_balance_versions_character_id", table_name="character_balance_versions")
    op.drop_table("character_balance_versions")
    op.drop_index("ix_characters_release_status", table_name="characters")
    op.drop_index("ix_characters_code", table_name="characters")
    op.drop_table("characters")
