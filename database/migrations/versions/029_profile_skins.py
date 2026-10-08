"""Persist the selected profile skin on each user.

Revision ID: 029_profile_skins
Revises: 028_invasion_defense_actions
Create Date: 2026-10-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029_profile_skins"
down_revision: Union[str, None] = "028_invasion_defense_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add a Green Apple default without changing any existing user data."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "profile_skin" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "profile_skin",
                sa.String(length=32),
                server_default="green_apple",
                nullable=False,
            ),
        )

    # The server default gives every pre-existing row Green Apple when the
    # column is added. Also repair any pre-release NULL/blank values safely.
    bind.execute(
        sa.text(
            "UPDATE users SET profile_skin = 'green_apple' "
            "WHERE profile_skin IS NULL OR TRIM(profile_skin) = ''"
        )
    )


def downgrade() -> None:
    """Remove only the preference column introduced by this revision."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "profile_skin" in columns:
        op.drop_column("users", "profile_skin")
