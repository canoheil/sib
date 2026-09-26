"""Add panel ownership table.

Revision ID: 008_panel_owners
Revises: 007_bot_player_nullable_group
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "008_panel_owners"
down_revision: Union[str, None] = "007_bot_player_nullable_group"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "panel_owners",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_type", sa.String(length=50), nullable=False, server_default="generic"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "message_id", name="pk_panel_owners"),
    )
    op.create_index("ix_panel_owners_owner_user_id", "panel_owners", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_panel_owners_owner_user_id", table_name="panel_owners")
    op.drop_table("panel_owners")
