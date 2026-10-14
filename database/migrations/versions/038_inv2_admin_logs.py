"""Create a durable admin audit table; existing admin actions only logged to stdout.

Revision ID: 038_inv2_admin_logs
Revises: 037_inv2_group_members
"""
from alembic import op
import sqlalchemy as sa

revision = "038_inv2_admin_logs"
down_revision = "037_inv2_group_members"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_admin_logs_admin_id", "admin_logs", ["admin_id"])
    op.create_index("ix_admin_logs_action", "admin_logs", ["action"])


def downgrade() -> None:
    op.drop_index("ix_admin_logs_action", table_name="admin_logs")
    op.drop_index("ix_admin_logs_admin_id", table_name="admin_logs")
    op.drop_table("admin_logs")
