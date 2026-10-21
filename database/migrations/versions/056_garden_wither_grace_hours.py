"""Garden withering grace period configurable, default 4h.

Revision ID: 056_garden_wither_grace_hours
Revises: 055_inv2_random_events
Create Date: 2026-10-21

G4.5 — make withering grace period configurable via system_config.
Default 4 hours (was hardcoded 12h), tunable without deploy.

The config key is seeded here and defaults to 4 via CONFIG_DEFAULTS.
Never overwrites an existing (possibly customized) value.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "056_garden_wither_grace_hours"
down_revision: Union[str, None] = "055_inv2_random_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('garden_wither_grace_hours', '4') "
                "ON DUPLICATE KEY UPDATE `key` = `key`"
            )
        )
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('garden_wither_grace_hours', '4') "
                "ON CONFLICT (`key`) DO NOTHING"
            )
        )
    else:
        raise RuntimeError(
            f"Unsupported garden_wither_grace_hours seed dialect: {dialect}"
        )


def downgrade() -> None:
    # The config key is intentionally kept: it may have been customized,
    # and the code default (4h) is safe.
    pass
