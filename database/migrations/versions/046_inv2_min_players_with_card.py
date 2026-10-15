"""Configurable minimum number of card-carrying members for an inv2 spawn.

Revision ID: 046_inv2_min_players_with_card
Revises: 045_group_welcomed_at

Config key description (system_config has only key/value/timestamps):
invasion_min_players_with_card — Minimum number of active group members who
must have a main card for invasion to spawn.

`key` and `value` are reserved words; every statement below backticks-quotes
them on every dialect (migration 040 was the pattern this follows).
"""
from alembic import op
import sqlalchemy as sa

revision = "046_inv2_min_players_with_card"
down_revision = "045_group_welcomed_at"
branch_labels = None
depends_on = None

_KEY = "invasion_min_players_with_card"
_VALUE = "3"


def upgrade() -> None:
    # ConfigService stores JSON scalar values as text. On duplicate, retain
    # existing administrator configuration; never modify invasion_enabled.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES "
            "('invasion_min_players_with_card', '3') "
            "ON DUPLICATE KEY UPDATE `key` = `key`"
        ))
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES "
            "('invasion_min_players_with_card', '3') "
            "ON CONFLICT (`key`) DO NOTHING"
        ))
    else:
        raise RuntimeError(f"Unsupported inv2 min-players seed dialect: {dialect}")


def downgrade() -> None:
    # Remove only the row this migration seeded, i.e. one still holding the
    # untouched default. A customized value is left in place, so downgrading
    # never destroys an administrator's setting; delete it by hand if wanted.
    # The in-code default is the same '3', so behaviour is identical either
    # way once the row is gone.
    op.execute(sa.text(
        "DELETE FROM system_config "
        "WHERE `key` = 'invasion_min_players_with_card' AND `value` = '3'"
    ))
