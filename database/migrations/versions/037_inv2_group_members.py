"""Track recently observed users in groups for authoritative inv2 power reads.

Revision ID: 037_inv2_group_members
Revises: 036_invasion_v2

Config key description (system_config has only key/value/timestamps):
inv2_member_active_window_days — Days after which a group member is considered inactive.
"""

import sqlalchemy as sa
from alembic import op

revision = "037_inv2_group_members"
down_revision = "036_invasion_v2"
branch_labels = None
depends_on = None

_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "inv2_group_members",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_inv2_group_members_user"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_inv2_group_members_group"),
        sa.UniqueConstraint("user_id", "group_id", name="uq_inv2_group_members_user_group"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_group_members_group_last_seen", "inv2_group_members", ["group_id", "last_seen_at"])
    op.create_index("ix_inv2_group_members_group_id", "inv2_group_members", ["group_id"])
    op.create_index("ix_inv2_group_members_user_id", "inv2_group_members", ["user_id"])

    # ConfigService stores JSON scalar values as text. On duplicate, retain
    # existing administrator configuration; never modify invasion_enabled.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES "
            "('inv2_member_active_window_days', '30') "
            "ON DUPLICATE KEY UPDATE `key` = `key`"
        ))
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES "
            "('inv2_member_active_window_days', '30') "
            "ON CONFLICT (`key`) DO NOTHING"
        ))
    else:
        raise RuntimeError(f"Unsupported inv2 group-members seed dialect: {dialect}")


def downgrade() -> None:
    # Do not delete the config key: it might predate this migration or have
    # been customized. Dropping only this table is reversible without data
    # loss in existing system_config.
    op.drop_index("ix_inv2_group_members_user_id", table_name="inv2_group_members")
    op.drop_index("ix_inv2_group_members_group_id", table_name="inv2_group_members")
    op.drop_index("ix_inv2_group_members_group_last_seen", table_name="inv2_group_members")
    op.drop_table("inv2_group_members")
