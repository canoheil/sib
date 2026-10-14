"""Persist the rendered Invasion v2 announcement narrative.

Phase 8D edits the announcement caption on every join to show the live
enemy HP. The edit must rebuild the caption from the exact narrative that
was sent, so the narrative is stored on the invasion row at announce time.
The column is nullable: pre-existing rows stay NULL and the edit path falls
back to a best-effort re-render.

No feature flag, active invasion snapshot or archived table is changed.

Revision ID: 041_inv2_announcement_narrative
Revises: 040_inv2_difficulty_default
"""
import sqlalchemy as sa
from alembic import op

revision = "041_inv2_announcement_narrative"
down_revision = "040_inv2_difficulty_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inv2_invasions",
        sa.Column("announcement_narrative", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inv2_invasions", "announcement_narrative")
