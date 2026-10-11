"""Shop chests: apple-priced chests with independent weighted reward tables.

Revision ID: 032_shop_chests
Revises: 031_invasion_four_wave
Create Date: 2026-10-10

Adds three tables and nothing else:

* ``shop_chests``           - chest definitions (code, name, image key, price,
                              duplicate compensation, active flag, order, version).
* ``shop_chest_rewards``    - an independent weighted reward table per chest.
* ``shop_chest_openings``   - one row per opening: the per-user idempotency gate
                              (unique ``user_id`` + ``operation_key``) and an
                              immutable snapshot of what was granted.

No rows are seeded: game-balance defaults (prices, weights, rewards) are
configuration, not schema. Daily chest tables and keys are not touched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "032_shop_chests"
down_revision: Union[str, None] = "031_invasion_four_wave"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shop_chests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("image_key", sa.String(length=120), nullable=True),
        sa.Column("price_apples", sa.Integer(), nullable=False),
        sa.Column("duplicate_apples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_shop_chests_code"),
        sa.CheckConstraint("price_apples >= 1", name="ck_shop_chests_price_positive"),
        sa.CheckConstraint("duplicate_apples >= 0", name="ck_shop_chests_duplicate_nonneg"),
        sa.CheckConstraint("version >= 1", name="ck_shop_chests_version_positive"),
    )
    op.create_index(
        "ix_shop_chests_active_sort", "shop_chests", ["is_active", "sort_order"]
    )

    op.create_table(
        "shop_chest_rewards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chest_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("apples", sa.Integer(), nullable=True),
        sa.Column("item_key", sa.String(length=64), nullable=True),
        sa.Column("character_id", sa.BigInteger(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["chest_id"], ["shop_chests.id"], name="fk_shop_chest_rewards_chest", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name="fk_shop_chest_rewards_character",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "kind IN ('apples', 'item', 'card')", name="ck_shop_chest_rewards_kind"
        ),
        sa.CheckConstraint("weight >= 1", name="ck_shop_chest_rewards_weight_positive"),
        sa.CheckConstraint(
            "(kind = 'apples' AND apples IS NOT NULL AND apples >= 1"
            " AND item_key IS NULL AND character_id IS NULL)"
            " OR (kind = 'item' AND item_key IS NOT NULL AND apples IS NULL"
            " AND character_id IS NULL)"
            " OR (kind = 'card' AND character_id IS NOT NULL AND apples IS NULL"
            " AND item_key IS NULL)",
            name="ck_shop_chest_rewards_shape",
        ),
    )
    op.create_index("ix_shop_chest_rewards_chest", "shop_chest_rewards", ["chest_id"])

    op.create_table(
        "shop_chest_openings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chest_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("chest_version", sa.Integer(), nullable=False),
        sa.Column("price_apples", sa.Integer(), nullable=False),
        sa.Column("reward_kind", sa.String(length=16), nullable=False),
        sa.Column("reward_apples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reward_item_key", sa.String(length=64), nullable=True),
        sa.Column("reward_character_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "duplicate_converted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("compensation_apples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("balance_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_shop_chest_openings_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chest_id"],
            ["shop_chests.id"],
            name="fk_shop_chest_openings_chest",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("user_id", "operation_key", name="uq_shop_chest_openings_user_key"),
        sa.CheckConstraint("price_apples >= 1", name="ck_shop_chest_openings_price_positive"),
        sa.CheckConstraint(
            "compensation_apples >= 0", name="ck_shop_chest_openings_comp_nonneg"
        ),
    )
    op.create_index("ix_shop_chest_openings_user_id", "shop_chest_openings", ["user_id"])
    op.create_index("ix_shop_chest_openings_chest", "shop_chest_openings", ["chest_id"])


def downgrade() -> None:
    # Children first: openings and rewards reference shop_chests.
    op.drop_index("ix_shop_chest_openings_chest", table_name="shop_chest_openings")
    op.drop_index("ix_shop_chest_openings_user_id", table_name="shop_chest_openings")
    op.drop_table("shop_chest_openings")

    op.drop_index("ix_shop_chest_rewards_chest", table_name="shop_chest_rewards")
    op.drop_table("shop_chest_rewards")

    op.drop_index("ix_shop_chests_active_sort", table_name="shop_chests")
    op.drop_table("shop_chests")
