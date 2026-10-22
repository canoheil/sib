"""Add leaderboard finalization persistence columns to halloween_event_state.

Revision ID: 058_halloween_leaderboard_finalized
Revises: 057_halloween_event
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa


revision = "058_halloween_leaderboard_finalized"
down_revision = "057_halloween_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "halloween_event_state",
        sa.Column(
            "leaderboard_finalized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "halloween_event_state",
        sa.Column(
            "leaderboard_finalized_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("halloween_event_state", "leaderboard_finalized_at")
    op.drop_column("halloween_event_state", "leaderboard_finalized")
