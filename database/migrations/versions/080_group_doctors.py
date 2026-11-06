"""Add per-group Doctor of the Day state table.

Revision ID: 080_group_doctors
Revises: 079_disease_daily_announcement
"""

import sqlalchemy as sa
from alembic import op

revision = "080_group_doctors"
down_revision = "079_disease_daily_announcement"
branch_labels = None
depends_on = None

_TABLE = "group_doctor_states"


def _has_table() -> bool:
    if op.get_context().as_sql:
        return False
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def upgrade() -> None:
    if not _has_table():
        op.create_table(
            _TABLE,
            sa.Column(
                "chat_id",
                sa.BigInteger(),
                primary_key=True,
                autoincrement=False,
            ),
            sa.Column(
                "doctor_user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "selected_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "heals_today",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )


def downgrade() -> None:
    if op.get_context().as_sql or _has_table():
        op.drop_table(_TABLE)
