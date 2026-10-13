"""Store verbatim official card affiliations without changing existing data.

Revision ID: 034_card_affiliations
Revises: 033_shop_card_price
Create Date: 2026-10-12

Only a nullable TEXT column is added. Existing factions, roles, card stats,
ownership, levels, favorites, wallets and balance history are untouched.
The seed sync, not this schema migration, applies officially supplied
content to explicitly marked cards.

The downgrade intentionally retains this additive column: removing it
would destroy official text. Rolling back the application is compatible
with the extra nullable column and does not require dropping it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034_card_affiliations"
down_revision: Union[str, None] = "033_shop_card_price"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A downgrade retains the data column, so upgrading again must not try
    # to add it twice. Offline SQL targets the usual pre-034 schema.
    if not op.get_context().as_sql:
        columns = sa.inspect(op.get_bind()).get_columns("characters")
        if any(column["name"] == "affiliations_fa" for column in columns):
            return
    op.add_column("characters", sa.Column("affiliations_fa", sa.Text(), nullable=True))


def downgrade() -> None:
    # Data-preserving rollback: do NOT drop the official affiliations.
    pass
