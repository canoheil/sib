"""Seed the seedling sell price (shop «فروش آیتم‌ها» -> «نهال»).

Revision ID: 061_seedling_sell_price
Revises: 060_invasion_fixed_schedule
Create Date: 2026-10-23

Players can sell EXTRA seedlings (the ones above their garden's planting
capacity) back to the shop for normal Apples. The unit price lives in
``system_config`` under ``seedling_sell_price`` so it can be rebalanced from
the admin panel without a deploy; ``CONFIG_DEFAULTS`` carries the same value
as the code fallback.

Balance basis (default 20 Apples):
* A seedling is a win reward and is NOT buyable, so the ceiling is what it
  produces. One seedling plants one tree; the tree's FIRST harvest at magic
  level 0 is worth, from ``garden_drop_weights["0"]`` x ``item_sell_prices``
  with ``garden_fruit_amount`` = 3 for the normal-Apple outcome:
      0.70*3 + 0.20*50 + 0.09*100 + 0.01*200 = 23.1 Apples
  Selling at 20 therefore pays less than even a single harvest, and the tree
  repeats that harvest every ``garden_fruit_seconds`` (12h) forever —
  planting always stays the better choice.
* It sits above the cheapest existing sell prices (flower 15, pumpkin_seed
  15) because a seedling costs a whole ranked win, while one casual friendly
  win already pays 10 Apples (``friendly_reward_apples_per_match``), and
  below Magic Powder's 40.

Like every earlier config seed (036, 046, 055, 056, 060), the insert never
overwrites an existing (possibly customized) value, and ``downgrade`` only
removes the row while it still holds the untouched default.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "061_seedling_sell_price"
down_revision: Union[str, None] = "060_invasion_fixed_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEY = "seedling_sell_price"
_VALUE = "20"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) "
                "ON DUPLICATE KEY UPDATE `key` = `key`"
            ).bindparams(key=_KEY, value=_VALUE)
        )
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) "
                "ON CONFLICT (`key`) DO NOTHING"
            ).bindparams(key=_KEY, value=_VALUE)
        )
    else:
        raise RuntimeError(f"Unsupported seedling_sell_price seed dialect: {dialect}")


def downgrade() -> None:
    # Remove only the row this migration seeded, i.e. while it still holds the
    # untouched default. A customized admin value is left in place (the code
    # default 20 stays valid either way).
    op.execute(
        sa.text(
            "DELETE FROM system_config WHERE `key` = :key AND `value` = :value"
        ).bindparams(key=_KEY, value=_VALUE)
    )
