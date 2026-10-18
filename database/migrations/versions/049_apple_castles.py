"""Apple castles, donation audit and global daily income gate."""

from alembic import op
import sqlalchemy as sa

revision = "049_apple_castles"
down_revision = "048_apple_friendships"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "group_castles",
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("total_donated", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("level BETWEEN 1 AND 10", name="ck_castle_level"),
        sa.CheckConstraint("total_donated >= 0", name="ck_castle_total"),
    )
    op.create_table(
        "castle_donations",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("group_castles.group_id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column(
            "donated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("request_key", sa.String(100), nullable=False, unique=True),
        sa.CheckConstraint("amount > 0", name="ck_castle_donation_amount"),
    )
    op.create_index(
        "ix_castle_donations_user_time", "castle_donations", ["user_id", "donated_at"]
    )
    op.create_index(
        "ix_castle_donations_group_user", "castle_donations", ["group_id", "user_id"]
    )
    op.create_table(
        "castle_income_claims",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("group_castles.group_id"),
            nullable=False,
        ),
        sa.Column("claim_day", sa.String(10), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "claim_day", name="uq_castle_income_user_day"),
    )


def downgrade():
    op.drop_table("castle_income_claims")
    op.drop_table("castle_donations")
    op.drop_table("group_castles")
