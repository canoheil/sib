"""Create the Apple Mine shift history table.

Revision ID: 043_apple_mine_shift
Revises: 042_inv2_pinned_announcement
"""
from alembic import op
import sqlalchemy as sa


revision = "043_apple_mine_shift"
down_revision = "042_inv2_pinned_announcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apple_mine_shift",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("card_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("worked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["card_id"], ["player_cards.id"], ondelete="SET NULL"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_apple_mine_shift_user_id", "apple_mine_shift", ["user_id"]
    )
    op.create_index(
        "ix_apple_mine_shift_worked_at", "apple_mine_shift", ["worked_at"]
    )
    op.create_index(
        "ix_apple_mine_shift_user_worked_at",
        "apple_mine_shift",
        ["user_id", "worked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_apple_mine_shift_user_worked_at", table_name="apple_mine_shift"
    )
    op.drop_index("ix_apple_mine_shift_worked_at", table_name="apple_mine_shift")
    op.drop_index("ix_apple_mine_shift_user_id", table_name="apple_mine_shift")
    op.drop_table("apple_mine_shift")
