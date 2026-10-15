"""Reversible administrative user ban metadata."""
from alembic import op
import sqlalchemy as sa
revision = "044_user_is_banned"
down_revision = "043_apple_mine_shift"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("users", sa.Column("is_banned", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("banned_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("banned_by", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("ban_reason", sa.String(255), nullable=True))
    op.create_foreign_key("fk_users_banned_by", "users", "users", ["banned_by"], ["id"])

def downgrade():
    op.drop_constraint("fk_users_banned_by", "users", type_="foreignkey")
    for column in ("ban_reason", "banned_by", "banned_at", "is_banned"):
        op.drop_column("users", column)
