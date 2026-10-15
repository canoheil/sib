"""Track the one-time group welcome message.

Revision ID: 045_group_welcomed_at
Revises: 044_user_is_banned
"""
from alembic import op
import sqlalchemy as sa

revision = "045_group_welcomed_at"
down_revision = "044_user_is_banned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("groups", sa.Column("welcomed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("groups", "welcomed_at")
