"""Allow bot match players without a Telegram group.

Revision ID: 007_bot_player_nullable_group
Revises: 006_bot_match_flag
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "007_bot_player_nullable_group"
down_revision: Union[str, None] = "006_bot_match_flag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "match_players",
        "group_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "match_players",
        "group_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
