"""Daily, curated official-channel digest with durable per-day idempotency.

Revision ID: 066_daily_channel_digest
Revises: 065_league_points
Create Date: 2026-10-26

The worker evaluates one concise league/activity digest at the configured
Asia/Tehran hour (20:00 by default). ``daily_channel_posts.post_day`` is the
idempotency key: sent, intentionally skipped, failed, and in-flight attempts
all remain visible after a restart, so quiet days do not get rechecked every
30 seconds and a restart cannot blindly repost the same digest.

Only the delivery record is new schema. Scores and daily activity remain
derived from existing match, war, mine, and duel tables. Configuration seeds
are insert-if-missing so an operator's values are never overwritten.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "066_daily_channel_digest"
down_revision: Union[str, None] = "065_league_points"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "daily_channel_posts"
_CONFIG_DEFAULTS = {
    "daily_digest_enabled": "true",
    "daily_digest_hour": "20",
}


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _config_value(key: str) -> str | None:
    row = op.get_bind().execute(
        sa.text("SELECT `value` FROM system_config WHERE `key` = :key").bindparams(
            key=key
        )
    ).first()
    return row[0] if row is not None else None


def _seed(key: str, value: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) ON DUPLICATE KEY UPDATE `key` = `key`"
            ).bindparams(key=key, value=value)
        )
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) ON CONFLICT (`key`) DO NOTHING"
            ).bindparams(key=key, value=value)
        )
    else:  # pragma: no cover - unsupported test dialect
        raise RuntimeError(f"Unsupported daily digest seed dialect: {dialect}")


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("post_day", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("channel_id", sa.String(length=100), nullable=False),
            sa.Column(
                "attempt_count", sa.Integer(), server_default="1", nullable=False
            ),
            sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("skip_reason", sa.String(length=120), nullable=True),
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
                "status IN ('sending', 'sent', 'skipped', 'failed')",
                name="ck_daily_channel_posts_status",
            ),
            sa.PrimaryKeyConstraint("post_day"),
        )

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    if _has_table(_TABLE):
        op.drop_table(_TABLE)

    for key, value in _CONFIG_DEFAULTS.items():
        if _config_value(key) == value:
            op.execute(
                sa.text("DELETE FROM system_config WHERE `key` = :key")
                .bindparams(key=key)
            )
