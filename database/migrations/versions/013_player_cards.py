"""Add player cards.

Revision ID: 013_player_cards
Revises: 012_characters
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "013_player_cards"
down_revision: Union[str, None] = "012_characters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_cards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("character_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("power", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("speed", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("intelligence", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("boosted_power", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("boosted_speed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("boosted_intelligence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "character_id", name="uq_player_card_user_character"),
    )
    op.create_index("ix_player_cards_user_id", "player_cards", ["user_id"])
    op.create_index("ix_player_cards_character_id", "player_cards", ["character_id"])


def downgrade() -> None:
    op.drop_index("ix_player_cards_character_id", table_name="player_cards")
    op.drop_index("ix_player_cards_user_id", table_name="player_cards")
    op.drop_table("player_cards")
