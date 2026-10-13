"""Add direct duel invitations and unique player reservations; preserve all data.

Revision ID: 035_apple_duel
Revises: 034_card_affiliations
"""

import sqlalchemy as sa
from alembic import op

revision = "035_apple_duel"
down_revision = "034_card_affiliations"
branch_labels = None
depends_on = None


def upgrade():
    # Frozen schema (do not import runtime models into historical migrations).
    # Match init_db/create_all convention; supports offline SQL too.
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(op.get_bind())
    if offline or not inspector.has_table("apple_duels"):
        op.create_table(
            "apple_duels",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("group_id", sa.BigInteger(), nullable=False),
            sa.Column("inviter_id", sa.BigInteger(), nullable=False),
            sa.Column("recipient_id", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "battle_id",
                sa.BigInteger(),
                sa.ForeignKey("apple_battles.id"),
                nullable=True,
                unique=True,
            ),
            sa.Column("snapshot_json", sa.Text(), nullable=True),
            sa.Column("cursor", sa.Integer(), nullable=False),
            sa.Column("next_scene_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("terminal_presented", sa.Boolean(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        for column in ("inviter_id", "recipient_id", "status", "expires_at"):
            op.create_index(f"ix_apple_duels_{column}", "apple_duels", [column])
    if offline or not inspector.has_table("apple_duel_slots"):
        op.create_table(
            "apple_duel_slots",
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id"),
                primary_key=True,
                autoincrement=False,
            ),
            sa.Column(
                "duel_id",
                sa.String(32),
                sa.ForeignKey("apple_duels.id"),
                nullable=False,
            ),
        )
        op.create_index("ix_apple_duel_slots_duel_id", "apple_duel_slots", ["duel_id"])


def downgrade():
    # Additive data-preserving rollback; old application ignores these tables.
    pass
