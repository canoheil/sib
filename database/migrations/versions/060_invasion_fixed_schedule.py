"""Switch invasion v2 spawning to a fixed daily schedule (Asia/Tehran).

Revision ID: 060_invasion_fixed_schedule
Revises: 059_halloween_seed_loot_boost
Create Date: 2026-10-23

The scheduler used to spawn an invasion ``invasion_interval_seconds`` after
the previous one, which drifted onto arbitrary clock hours over time. This
revision seeds the two new config keys the fixed-hour scheduler reads
(services/invasion_v2/scheduler.py):

* ``invasion_spawn_hours`` — comma-separated Asia/Tehran local hours, default
  ``"0,12,16,20"`` (midnight, noon, 16:00, 20:00).
* ``invasion_slot_window_minutes`` — how many minutes into the hour a tick
  still counts as "in slot", default ``"30"``.

``invasion_interval_seconds`` itself is left untouched: it is still a valid
config row, just no longer read for timing.

It also adds ``inv2_group_state.last_spawn_slot`` (nullable timestamptz),
the per-group marker the scheduler uses to guarantee at most one spawn per
group per fixed slot, surviving restarts and overlapping maintenance ticks.

Like every earlier invasion-config seed (migrations 036, 040, 046, 055), the
inserts never overwrite an existing (possibly customized) value, and
``downgrade`` only removes rows that still hold the untouched default.
"""

from alembic import op
import sqlalchemy as sa


revision = "060_invasion_fixed_schedule"
down_revision = "059_halloween_seed_loot_boost"
branch_labels = None
depends_on = None

_SPAWN_HOURS_KEY = "invasion_spawn_hours"
_SPAWN_HOURS_VALUE = "0,12,16,20"
_SLOT_WINDOW_KEY = "invasion_slot_window_minutes"
_SLOT_WINDOW_VALUE = "30"


def _seed(key: str, value: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) "
            "ON DUPLICATE KEY UPDATE `key` = `key`"
        ).bindparams(key=key, value=value))
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) "
            "ON CONFLICT (`key`) DO NOTHING"
        ).bindparams(key=key, value=value))
    else:
        raise RuntimeError(f"Unsupported invasion fixed-schedule seed dialect: {dialect}")


def upgrade() -> None:
    _seed(_SPAWN_HOURS_KEY, _SPAWN_HOURS_VALUE)
    _seed(_SLOT_WINDOW_KEY, _SLOT_WINDOW_VALUE)
    op.add_column(
        "inv2_group_state",
        sa.Column("last_spawn_slot", sa.DateTime(timezone=True), nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("inv2_group_state", "last_spawn_slot")
    # Remove only the rows this migration seeded, i.e. still holding the
    # untouched default. A customized admin value is left in place.
    op.execute(sa.text(
        "DELETE FROM system_config WHERE `key` = :key AND `value` = :value"
    ).bindparams(key=_SPAWN_HOURS_KEY, value=_SPAWN_HOURS_VALUE))
    op.execute(sa.text(
        "DELETE FROM system_config WHERE `key` = :key AND `value` = :value"
    ).bindparams(key=_SLOT_WINDOW_KEY, value=_SLOT_WINDOW_VALUE))
