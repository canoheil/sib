"""Track the pinned Invasion v2 announcement per group.

A new invasion pins its announcement so the whole group sees it, then unpins
the group's previous announcement. ``pinned_at`` records which announcement
the bot currently keeps pinned: it is stamped only after Telegram accepts the
pin and cleared when that message is unpinned. The column is nullable, so
pre-existing rows simply count as "not pinned by us" and are never unpinned.

No feature flag, active invasion snapshot or archived table is changed.

Revision ID: 042_inv2_pinned_announcement
Revises: 041_inv2_announcement_narrative
"""
import sqlalchemy as sa
from alembic import op

revision = "042_inv2_pinned_announcement"
down_revision = "041_inv2_announcement_narrative"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inv2_invasions",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inv2_invasions", "pinned_at")
