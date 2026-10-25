"""Come-back reminders: per-player state table + config seeds.

Revision ID: 063_reminders
Revises: 062_transfers
Create Date: 2026-10-25

Nothing brought a player back to sib: the chest, the garden and the invasion
all wait silently. ``services/reminders.py`` sends at most one reminder per
player per day (chest still closed / garden ready to harvest), and this new
table is what makes that promise keepable:

* ``last_sent_at`` + ``last_kind`` — the per-player gap and the audit of what
  was last sent;
* ``blocked_at`` — Telegram answered ``Forbidden`` (player blocked the bot);
  never message them again;
* ``opted_out`` — the player switched reminders off from the profile.

One NEW table only; no existing table or row is altered. The config seeds use
insert-if-missing, so an operator's customised values are never overwritten,
and ``downgrade`` removes only the rows that still hold the untouched default.
The revision is re-runnable: ``upgrade`` checks for the table first.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "063_reminders"
down_revision: Union[str, None] = "062_transfers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "user_reminder_states"
_BLOCKED_INDEX = "ix_user_reminder_states_blocked_at"
_LAST_SENT_INDEX = "ix_user_reminder_states_last_sent_at"

# key -> seeded default (system_config values are JSON-decoded by
# ConfigService, so "true" becomes a bool and "20" an int).
_CONFIG_DEFAULTS: dict[str, str] = {
    "reminders_enabled": "true",
    "reminders_min_gap_hours": "20",
    # Asia/Tehran local hours during which reminders may be sent at all.
    "reminders_window_start_hour": "16",
    "reminders_window_end_hour": "22",
    # The chest nudge only starts after this local hour (morning is not
    # "you forgot your chest", it is "the day just started").
    "reminders_chest_after_hour": "17",
    # One maintenance tick never messages more than this many players.
    "reminders_batch_size": "25",
}


def _seed(key: str, value: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) "
                "ON DUPLICATE KEY UPDATE `key` = `key`"
            ).bindparams(key=key, value=value)
        )
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) "
                "ON CONFLICT (`key`) DO NOTHING"
            ).bindparams(key=key, value=value)
        )
    else:  # pragma: no cover - unsupported test dialect
        raise RuntimeError(f"Unsupported reminders config seed dialect: {dialect}")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column(
                "last_sent_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("last_kind", sa.String(length=32), nullable=True),
            sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "opted_out",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="fk_user_reminder_states_user",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("user_id"),
        )
        op.create_index(_LAST_SENT_INDEX, _TABLE, ["last_sent_at"])
        op.create_index(_BLOCKED_INDEX, _TABLE, ["blocked_at"])

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text(
                "DELETE FROM system_config WHERE `key` = :key AND `value` = :value"
            ).bindparams(key=key, value=value)
        )
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
