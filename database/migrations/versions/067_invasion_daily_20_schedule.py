"""Default invasion v2 to one 20:00 Asia/Tehran slot per day.

Revision ID: 067_invasion_daily_20_schedule
Revises: 066_daily_channel_digest
Create Date: 2026-10-26

Migration 060 introduced four default spawn hours. Keep the scheduler
configurable, but move installations still using that untouched default to
one daily 20:00 Tehran slot. Any operator-selected schedule is preserved.
No schema or existing invasion rows are changed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "067_invasion_daily_20_schedule"
down_revision: Union[str, None] = "066_daily_channel_digest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEY = "invasion_spawn_hours"
_OLD_RAW = "0,12,16,20"
_OLD_JSON = '"0,12,16,20"'
_NEW_JSON = '"20"'


def upgrade() -> None:
    """Change only the old seed, accepting both legacy and JSON string forms."""
    op.execute(
        sa.text(
            "UPDATE system_config SET `value` = :new_value "
            "WHERE `key` = :key AND `value` IN (:old_raw, :old_json)"
        ).bindparams(
            key=_KEY,
            old_raw=_OLD_RAW,
            old_json=_OLD_JSON,
            new_value=_NEW_JSON,
        )
    )


def downgrade() -> None:
    """Restore only the schedule this migration installed as its default."""
    op.execute(
        sa.text(
            "UPDATE system_config SET `value` = :old_value "
            "WHERE `key` = :key AND `value` = :new_value"
        ).bindparams(key=_KEY, old_value=_OLD_RAW, new_value=_NEW_JSON)
    )
