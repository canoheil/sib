"""Seed the daily Disease & Doctor group announcement configuration.

The maintenance worker checks the configured Asia/Tehran time (18:15 by
default, shortly after the 18:00 infection tick) and posts one announcement
per active group naming the Doctor of the Day and that group's sick players.
``last_disease_announcement_date`` in ``system_config`` is the idempotency
key the worker claims before sending, so a restart cannot repost the same
day. The value is pure configuration, so this migration only seeds
``system_config`` and touches no table.

Revision ID: 079_disease_daily_announcement
Revises: 078_wanted_decay
"""

import sqlalchemy as sa
from alembic import op

revision = "079_disease_daily_announcement"
down_revision = "078_wanted_decay"
branch_labels = None
depends_on = None

_CONFIG_DEFAULTS = {
    "disease_announcement_hour": "18",
    "disease_announcement_minute": "15",
    "disease_announcement_enabled": "true",
}


def _seed(key: str, value: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        conflict_clause = "ON DUPLICATE KEY UPDATE `key` = `key`"
    elif dialect == "sqlite":
        conflict_clause = "ON CONFLICT (`key`) DO NOTHING"
    else:
        raise RuntimeError(f"Unsupported disease announcement config dialect: {dialect}")
    op.execute(
        sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) "
            + conflict_clause
        ).bindparams(key=key, value=value)
    )


def upgrade() -> None:
    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    # Never erase an operator override; equality with the seeded value is the
    # available proxy for an untouched default. Compare bytes rather than
    # using a potentially case-insensitive database collation.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        predicate = "BINARY `key` = BINARY :key AND BINARY `value` = BINARY :value"
    elif dialect == "sqlite":
        predicate = "`key` = :key COLLATE BINARY AND `value` = :value COLLATE BINARY"
    else:
        raise RuntimeError(f"Unsupported disease announcement config dialect: {dialect}")
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
                key=key, value=value
            )
        )
