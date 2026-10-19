"""Friend referral attribution on users (REF1).

Adds ``users.referred_by_id`` (who invited this player, set once at
registration through a ``?start=ref_<id>`` deep link) and
``users.referral_rewarded`` (whether the one-time inviter/invitee apple
reward for this referral has already been paid).
"""

from alembic import op
import sqlalchemy as sa

revision = "051_referrals"
down_revision = "050_apple_clash"
branch_labels = None
depends_on = None


def upgrade():
    # batch mode keeps the self-referencing foreign key portable (MariaDB
    # in production, SQLite in the isolated migration tests).
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("referred_by_id", sa.BigInteger(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "referral_rewarded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.create_foreign_key(
            "fk_users_referred_by", "users", ["referred_by_id"], ["id"]
        )
    op.create_index("ix_users_referred_by_id", "users", ["referred_by_id"])


def downgrade():
    op.drop_index("ix_users_referred_by_id", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_referred_by", type_="foreignkey")
        batch.drop_column("referral_rewarded")
        batch.drop_column("referred_by_id")
