"""Lower only the unchanged Invasion v2 difficulty default from 4.0 to 2.5.

Admin overrides are preserved: upgrade's WHERE clause matches only the exact
previous default ('4.0'), not arbitrary numeric-equivalent strings or other values.
Downgrade similarly touches only '2.5'; it cannot distinguish an admin explicitly
choosing that exact value from the new default. Missing rows remain missing.
No feature flag, active invasion snapshot or archived table is changed.

Revision ID: 040_inv2_difficulty_default
Revises: 039_archive_inv1_tables
"""
from alembic import op
import sqlalchemy as sa

revision = "040_inv2_difficulty_default"
down_revision = "039_archive_inv1_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE system_config
        SET `value` = '2.5'
        WHERE `key` = 'invasion_difficulty_multiplier'
          AND `value` = '4.0'
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        UPDATE system_config
        SET `value` = '4.0'
        WHERE `key` = 'invasion_difficulty_multiplier'
          AND `value` = '2.5'
    """))
