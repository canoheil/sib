"""Four-wave Invasion battles: heartbeat, reward marker, presentation stage.

Revision ID: 031_invasion_four_wave
Revises: 030_daily_chest
Create Date: 2026-10-09

Adds three nullable Invasion columns used by the activation-relative battle
timeline, and one database-level uniqueness rule for defense actions.

Data policy: already-FINISHED historical Invasions are marked as reward-
settled (``rewards_settled_at = ended_at``). Before this revision no code path
paid Invasion rewards, so settling history retroactively would silently inject
Golden Apples (apple_gold) that were never part of those events. New FINISHED
events stay pending until the settlement step pays them.

Legacy ACTIVE/SCHEDULED rows keep ``heartbeat_at`` and ``presentation_stage``
NULL; the runtime treats NULL as "legacy timing/presentation".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "031_invasion_four_wave"
down_revision: Union[str, None] = "030_daily_chest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFENSE_UNIQUE = "uq_invasion_defense_actions_participant_wave_action"
_SETTLED_INDEX = "ix_invasions_status_rewards_settled_at"


def upgrade() -> None:
    with op.batch_alter_table("invasions") as batch:
        batch.add_column(
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("rewards_settled_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("presentation_stage", sa.Integer(), nullable=True))
    op.create_index(
        _SETTLED_INDEX,
        "invasions",
        ["status", "rewards_settled_at"],
    )
    op.execute(
        sa.text(
            "UPDATE invasions "
            "SET rewards_settled_at = COALESCE(ended_at, CURRENT_TIMESTAMP) "
            "WHERE status = 'FINISHED' AND rewards_settled_at IS NULL"
        )
    )
    with op.batch_alter_table("invasion_defense_actions") as batch:
        batch.create_unique_constraint(
            _DEFENSE_UNIQUE,
            ["participant_id", "wave_id", "action_code"],
        )


def downgrade() -> None:
    with op.batch_alter_table("invasion_defense_actions") as batch:
        batch.drop_constraint(_DEFENSE_UNIQUE, type_="unique")
    op.drop_index(_SETTLED_INDEX, table_name="invasions")
    with op.batch_alter_table("invasions") as batch:
        batch.drop_column("presentation_stage")
        batch.drop_column("rewards_settled_at")
        batch.drop_column("heartbeat_at")
