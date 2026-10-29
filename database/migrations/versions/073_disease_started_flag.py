"""Seed the one-time initial disease wave flag.

Revision ID: 073_disease_started_flag
Revises: 072_disease_ui
"""

import sqlalchemy as sa
from alembic import op

revision = "073_disease_started_flag"
down_revision = "072_disease_ui"
branch_labels = None
depends_on = None

_KEY = "disease_initial_wave_triggered"
_VALUE = "false"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        conflict = "ON DUPLICATE KEY UPDATE `key` = `key`"
    elif dialect == "sqlite":
        conflict = "ON CONFLICT (`key`) DO NOTHING"
    else:
        raise RuntimeError(f"Unsupported disease wave config dialect: {dialect}")
    op.execute(
        sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) " + conflict
        ).bindparams(key=_KEY, value=_VALUE)
    )


def downgrade() -> None:
    # Never erase a completed wave or an in-progress claim on downgrade.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        predicate = "BINARY `key` = BINARY :key AND BINARY `value` = BINARY :value"
    elif dialect == "sqlite":
        predicate = "`key` = :key COLLATE BINARY AND `value` = :value COLLATE BINARY"
    else:
        raise RuntimeError(f"Unsupported disease wave config dialect: {dialect}")
    op.execute(
        sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
            key=_KEY, value=_VALUE
        )
    )
