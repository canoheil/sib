"""Create Apple friendship requests and accepted friendships.

Revision ID: 048_apple_friendships
Revises: 047_inv2_rebalance_sib_buff
"""
from alembic import op
import sqlalchemy as sa

revision = "048_apple_friendships"
down_revision = "047_inv2_rebalance_sib_buff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apple_friendships",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("friend_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["friend_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_apple_friendships_user_status", "apple_friendships", ["user_id", "status"])
    op.create_index("ix_apple_friendships_friend_status", "apple_friendships", ["friend_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_apple_friendships_friend_status", table_name="apple_friendships")
    op.drop_index("ix_apple_friendships_user_status", table_name="apple_friendships")
    op.drop_table("apple_friendships")
