"""Halloween HW2 balance pass: widen the group chest seed cap to 6.

Revision ID: 059_halloween_seed_loot_boost
Revises: 058_halloween_leaderboard_finalized
Create Date: 2026-10-22

Phase HW2 raised every Halloween seed payout from ``1–3`` to ``3–6``
``pumpkin_seed`` (Boss Menu «دریافت غنیمت» and the random group chests).
``halloween_loot_drop_claims.seeds_granted`` carried a
``CHECK (seeds_granted BETWEEN 1 AND 3)`` guard, so a 6-seed chest winner
would have been rejected by the database. This revision rewrites only that
check constraint: no row is touched and the historical floor of ``1`` is kept,
so every pre-HW2 row stays valid.

``batch_alter_table`` makes the swap work on both engines the project runs:
SQLite copies the table, MariaDB emits native ``DROP CHECK`` / ``ADD CHECK``.
Every constraint here is explicitly named, so no naming convention is needed.
"""

from alembic import op
import sqlalchemy as sa


revision = "059_halloween_seed_loot_boost"
down_revision = "058_halloween_leaderboard_finalized"
branch_labels = None
depends_on = None

_TABLE = "halloween_loot_drop_claims"
_CONSTRAINT = "ck_hw_drop_claims_seeds"
HW2_CHECK = "seeds_granted BETWEEN 1 AND 6"
HW1_CHECK = "seeds_granted BETWEEN 1 AND 3"


def _swap_check(*, condition: str) -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, condition)


def upgrade() -> None:
    _swap_check(condition=HW2_CHECK)


def downgrade() -> None:
    # Restoring the HW1 cap is only safe while no winner row exceeds it.
    above = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE seeds_granted > 3")
    ).scalar_one()
    if above:
        raise RuntimeError(
            f"Cannot restore {(_CONSTRAINT)}: {above} chest claim row(s) paid "
            "more than 3 seeds (HW2 balance pass). Lower them first."
        )
    _swap_check(condition=HW1_CHECK)
