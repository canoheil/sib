"""Apple Mine pickaxe progression: user_pickaxes + shift level snapshot.

Revision ID: 053_apple_pickaxes
Revises: 052_war_attack_count

Design: docs/games/M2-RECON.md (section 1.5). ``user_pickaxes`` is a 1:1
progression table, lazily created per user (every existing player is Level 1
on first read, so there is nothing to backfill). ``apple_mine_shift`` gains
a nullable ``pickaxe_level`` snapshot so every shift records the level it
was played with; pre-M2 rows keep ``NULL``.
"""
from alembic import op
import sqlalchemy as sa


revision = "053_apple_pickaxes"
down_revision = "052_war_attack_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_pickaxes",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "level",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
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
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_pickaxes_level", "user_pickaxes", ["level"])
    with op.batch_alter_table("apple_mine_shift") as batch:
        batch.add_column(sa.Column("pickaxe_level", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("apple_mine_shift") as batch:
        batch.drop_column("pickaxe_level")
    op.drop_index("ix_user_pickaxes_level", table_name="user_pickaxes")
    op.drop_table("user_pickaxes")
