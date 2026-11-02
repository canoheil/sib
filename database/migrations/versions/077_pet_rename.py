"""Add the one-time pet-rename marker to user_pets.

A pet may be renamed exactly once (see ``pet_service.rename_pet``), for a fixed
apple cost. ``has_renamed`` records that the paid rename was already used, so
the rule survives restarts and cannot be replayed by pressing the button again.

Revision ID: 077_pet_rename
Revises: 076_pet_system
"""

import sqlalchemy as sa
from alembic import op

revision = "077_pet_rename"
down_revision = "076_pet_system"
branch_labels = None
depends_on = None

_TABLE = "user_pets"
_COLUMN = "has_renamed"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return False
    return _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
