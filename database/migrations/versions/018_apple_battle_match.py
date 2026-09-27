"""Link apple battles to shared matches.

Adds an additive nullable ``match_id`` column so an Apple Battle record can
reference the shared ``matches`` row created by the real matchmaking flow.

Revision ID: 018_apple_battle_match
Revises: 017_assets_items
Create Date: 2026-09-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "018_apple_battle_match"
down_revision: Union[str, None] = "017_assets_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "apple_battles",
        sa.Column("match_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_apple_battles_match_id", "apple_battles", ["match_id"]
    )
    op.create_foreign_key(
        "fk_apple_battles_match_id",
        "apple_battles",
        "matches",
        ["match_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_apple_battles_match_id", "apple_battles", type_="foreignkey"
    )
    op.drop_index("ix_apple_battles_match_id", table_name="apple_battles")
    op.drop_column("apple_battles", "match_id")
