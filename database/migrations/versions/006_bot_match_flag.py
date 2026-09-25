"""Add bot match flag to matches.

Revision ID: 006_bot_match_flag
Revises: 005_monetization_and_ads
Create Date: 2026-09-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006_bot_match_flag"
down_revision: Union[str, None] = "005_monetization_and_ads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "matches",
        sa.Column(
            "is_bot_match",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("matches", "is_bot_match")
