"""Durable per-group Golden Hour announcements.

Revision ID: 069_golden_hour_notices
Revises: 068_golden_hour_apple_rewards
Create Date: 2026-10-26

Each active Telegram group gets at most one notice per Asia/Tehran date.
Invasion announcements can carry the notice in their existing photo caption;
otherwise the maintenance worker sends a short standalone message after the
20:00 spawn window closes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "069_golden_hour_notices"
down_revision: Union[str, None] = "068_golden_hour_apple_rewards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "golden_hour_group_announcements"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table(_TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("announcement_day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("invasion_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed', 'skipped')",
            name="ck_golden_hour_group_announcements_status",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_golden_hour_group_announcements_group",
        ),
        sa.PrimaryKeyConstraint("group_id", "announcement_day"),
    )


def downgrade() -> None:
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
