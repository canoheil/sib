"""Unified shop card price: every card costs 300 Apples.

Revision ID: 033_shop_card_price
Revises: 032_shop_chests
Create Date: 2026-10-12

The shop no longer has a rarity system and no longer prices cards
individually: ``services/shop.py::ShopService.card_price()`` is the single
authoritative price (``SHOP_CARD_PRICE_APPLES``, defined in
``database/models/shop.py``), and the purchase transaction debits exactly
that value. This migration brings the STORED data in line with the rule so
no legacy value can even look like a per-card price:

1. ``characters.price`` - every row becomes the unified price. The column
   itself is deliberately KEPT (backward-compatible schema + historical
   data), but nothing reads it for pricing any more.
2. ``shop_entries.price_override`` - every historical per-entry override is
   cleared. The column is kept for the same reason.
3. ``characters.price``'s database-level default (still 100 from migration
   012) becomes the unified price, so a raw insert that omits the column
   cannot create a row that contradicts the rule. SQLite - the test engine
   only, never a deployment target - cannot alter column defaults; the code
   never relies on that default, so it is skipped there.

No data is destroyed: the pre-migration per-card prices remain readable in
the immutable balance history (``character_balance_versions.snapshot``,
which records ``price`` per version), so the change is auditable.

Downgrade therefore reverses only the schema default (there is no
meaningful "old price" to restore: the values were arbitrary and are
preserved in the balance snapshots). Player wallets, cards, ownership and
every other table are untouched by this migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "033_shop_card_price"
down_revision: Union[str, None] = "032_shop_chests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors database/models/shop.py::SHOP_CARD_PRICE_APPLES. Migrations are
# frozen snapshots of a moment in time and must not import application code
# that can change later, so the value is repeated here on purpose.
UNIFIED_CARD_PRICE_APPLES = 300
LEGACY_PRICE_DEFAULT = 100


def _supports_column_default_change() -> bool:
    """MariaDB/MySQL can ALTER COLUMN SET DEFAULT; SQLite (tests) cannot."""
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Every character/card: the one unified price.
    conn.execute(
        sa.text(
            "UPDATE characters SET price = :price WHERE price IS NULL OR price <> :price"
        ),
        {"price": UNIFIED_CARD_PRICE_APPLES},
    )

    # 2. Per-entry overrides are gone (no code path reads them).
    conn.execute(
        sa.text("UPDATE shop_entries SET price_override = NULL WHERE price_override IS NOT NULL")
    )

    # 3. Align the database-level default with the rule.
    if _supports_column_default_change():
        op.alter_column(
            "characters",
            "price",
            existing_type=sa.Integer(),
            server_default=str(UNIFIED_CARD_PRICE_APPLES),
            existing_nullable=False,
        )


def downgrade() -> None:
    if _supports_column_default_change():
        op.alter_column(
            "characters",
            "price",
            existing_type=sa.Integer(),
            server_default=str(LEGACY_PRICE_DEFAULT),
            existing_nullable=False,
        )

    # The data normalization is intentionally NOT reversed: cards now sell
    # for one global price, so there is no per-card price to restore, and
    # rewriting every row back would only invent data. The pre-migration
    # values stay available in character_balance_versions.snapshot.
