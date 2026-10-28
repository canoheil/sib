"""Seed the independent mine plant drop chance.

Revision ID: 071_disease_items
Revises: 070_disease_system
"""

import sqlalchemy as sa
from alembic import op

revision = "071_disease_items"
down_revision = "070_disease_system"
branch_labels = None
depends_on = None

_KEY = "disease_mine_plant_drop_chance"
_VALUE = "15"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        conflict = "ON DUPLICATE KEY UPDATE `key` = `key`"
    elif dialect == "sqlite":
        conflict = "ON CONFLICT (`key`) DO NOTHING"
    else:
        raise RuntimeError(f"Unsupported disease item config dialect: {dialect}")
    op.execute(
        sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) "
            + conflict
        ).bindparams(key=_KEY, value=_VALUE)
    )


def downgrade() -> None:
    # As in 070, preserve operator overrides; byte comparison avoids
    # accidentally deleting a different value under a case-insensitive collation.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        predicate = "BINARY `key` = BINARY :key AND BINARY `value` = BINARY :value"
    elif dialect == "sqlite":
        predicate = "`key` = :key COLLATE BINARY AND `value` = :value COLLATE BINARY"
    else:
        raise RuntimeError(f"Unsupported disease item config dialect: {dialect}")
    op.execute(
        sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
            key=_KEY, value=_VALUE
        )
    )
