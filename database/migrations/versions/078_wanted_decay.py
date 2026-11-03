"""Seed the GTA-style wanted-star decay interval.

Instead of all wanted stars expiring at once 24 hours after the last theft,
one star now decays every ``theft_star_decay_hours`` hours without a new
theft (see ``services/theft_service``). The value is pure configuration, so
this migration only seeds ``system_config`` and touches no table.

Revision ID: 078_wanted_decay
Revises: 077_pet_rename
"""

import sqlalchemy as sa
from alembic import op

revision = "078_wanted_decay"
down_revision = "077_pet_rename"
branch_labels = None
depends_on = None

_KEY = "theft_star_decay_hours"
_VALUE = "3"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        conflict = "ON DUPLICATE KEY UPDATE `key` = `key`"
    elif dialect == "sqlite":
        conflict = "ON CONFLICT (`key`) DO NOTHING"
    else:
        raise RuntimeError(f"Unsupported wanted-decay config dialect: {dialect}")
    op.execute(
        sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) " + conflict
        ).bindparams(key=_KEY, value=_VALUE)
    )


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
        raise RuntimeError(f"Unsupported wanted-decay config dialect: {dialect}")
    op.execute(
        sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
            key=_KEY, value=_VALUE
        )
    )
