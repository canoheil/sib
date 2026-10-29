"""Seed the shared daily doctor selection and healing quota state.

Revision ID: 072_disease_ui
Revises: 071_disease_items
"""

import sqlalchemy as sa
from alembic import op

revision = "072_disease_ui"
down_revision = "071_disease_items"
branch_labels = None
depends_on = None

_KEY = "disease_doctor_state"
_VALUE = '{"day":null,"user_id":null,"heals":0}'


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        conflict = "ON DUPLICATE KEY UPDATE `key` = `key`"
    elif dialect == "sqlite":
        conflict = "ON CONFLICT (`key`) DO NOTHING"
    else:
        raise RuntimeError(f"Unsupported doctor config dialect: {dialect}")
    op.execute(
        sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) " + conflict
        ).bindparams(key=_KEY, value=_VALUE)
    )


def downgrade() -> None:
    # Preserve a selection that was already made or an operator override.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        predicate = "BINARY `key` = BINARY :key AND BINARY `value` = BINARY :value"
    elif dialect == "sqlite":
        predicate = "`key` = :key COLLATE BINARY AND `value` = :value COLLATE BINARY"
    else:
        raise RuntimeError(f"Unsupported doctor config dialect: {dialect}")
    op.execute(
        sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
            key=_KEY, value=_VALUE
        )
    )
