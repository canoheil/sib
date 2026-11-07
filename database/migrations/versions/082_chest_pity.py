"""Add tiered shop chests, per-user pity and immutable reveal metadata.

Revision ID: 082_chest_pity
Revises: 081_card_rarity

Existing custom chests and opening history are retained. New tier defaults
are registered after roster seeding by the application, not by this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "082_chest_pity"
down_revision = "081_card_rarity"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    if op.get_context().as_sql:
        return False
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("shop_chests", "tier"):
        op.add_column("shop_chests", sa.Column("tier", sa.String(16), nullable=True))
    # Restore the reserved tier codes on an upgrade after a downgrade.
    chests = sa.table("shop_chests", sa.column("code", sa.String(40)), sa.column("tier", sa.String(16)))
    for tier in ("BRONZE", "SILVER", "GOLD", "DIAMOND"):
        op.execute(chests.update().where(chests.c.code == f"{tier.lower()}_chest").values(tier=tier))
    if not _has_column("shop_chest_openings", "reward_rarity"):
        op.add_column("shop_chest_openings", sa.Column("reward_rarity", sa.String(20), nullable=True))
    if not _has_column("shop_chest_openings", "pity_guaranteed"):
        op.add_column("shop_chest_openings", sa.Column(
            "pity_guaranteed", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))
    if op.get_context().as_sql or not sa.inspect(op.get_bind()).has_table("shop_chest_pity"):
        op.create_table(
            "shop_chest_pity",
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                      primary_key=True, autoincrement=False),
            sa.Column("pity_counter", sa.Integer(), nullable=False, server_default="0"),
            sa.CheckConstraint("pity_counter >= 0", name="ck_shop_chest_pity_nonneg"),
        )


def downgrade() -> None:
    if op.get_context().as_sql or sa.inspect(op.get_bind()).has_table("shop_chest_pity"):
        op.drop_table("shop_chest_pity")
    for table, columns in (
        ("shop_chest_openings", ("pity_guaranteed", "reward_rarity")),
        ("shop_chests", ("tier",)),
    ):
        for column in columns:
            if op.get_context().as_sql or _has_column(table, column):
                with op.batch_alter_table(table) as batch:
                    batch.drop_column(column)
