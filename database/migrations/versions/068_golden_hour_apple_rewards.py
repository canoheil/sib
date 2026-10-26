"""Record the golden-hour multiplier on Apple ledger entries.

Revision ID: 068_golden_hour_apple_rewards
Revises: 067_invasion_daily_20_schedule
Create Date: 2026-10-26

Existing ledger entries are ordinary credits/debits (multiplier 1). The
column records the applied factor for new eligible rewards, so an idempotent
retry after the 20:00–21:00 Tehran window validates against the original
payout instead of recalculating it from the retry time.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "068_golden_hour_apple_rewards"
down_revision: Union[str, None] = "067_invasion_daily_20_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "apple_transactions",
        sa.Column(
            "reward_multiplier",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("apple_transactions", "reward_multiplier")
