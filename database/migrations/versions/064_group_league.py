"""Weekly group league: podium + first-week guard, and config seeds.

Revision ID: 064_group_league
Revises: 063_reminders
Create Date: 2026-10-25

The league score is *derived* from ``matches`` (normal, non-bot, finished
matches inside a Tehran Saturday-to-Friday week), so this migration adds no
per-match columns and no counters that could drift. It adds only what cannot
be derived:

* ``group_league_weeks`` — PK ``week_key``; the single row that claims a week
  for finalization (atomic, re-runnable, and the reason a restart can never
  pay the same week twice);
* ``group_league_awards`` — the frozen podium (rank, points, wins, draws,
  losses, matches) plus ``players_paid`` / ``apples_per_player`` for the audit
  trail of what was actually paid.

``league_first_week`` is seeded with the week the migration runs in, so the
first deploy never pays a retroactive prize for a week that ended before the
feature existed.

Config seeds use insert-if-missing: an operator's customised value is never
overwritten, and ``downgrade`` removes only rows that still hold the untouched
default.
"""

from datetime import datetime, timedelta
from typing import Sequence, Union
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "064_group_league"
down_revision: Union[str, None] = "063_reminders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WEEKS = "group_league_weeks"
_AWARDS = "group_league_awards"
_TEHRAN = ZoneInfo("Asia/Tehran")
#: Saturday (Monday == 0). The Persian week starts on Saturday.
_WEEK_START_WEEKDAY = 5


def _current_week_key() -> str:
    """The Tehran Saturday date of the week this migration runs in."""
    local = datetime.now(_TEHRAN)
    days_since_saturday = (local.weekday() - _WEEK_START_WEEKDAY) % 7
    return (local - timedelta(days=days_since_saturday)).date().isoformat()


_CONFIG_DEFAULTS: dict[str, str] = {
    # Kill switch: off means the panel still renders, but no week is ever
    # finalized and no Apple is paid.
    "league_enabled": "true",
    # Apples per PARTICIPATING player of the rank (not per group).
    "league_rewards": '{"1": 300, "2": 150, "3": 100}',
    # A group must earn at least this many points to be crowned at all, so a
    # quiet week produces no champion instead of a 0-point winner.
    "league_min_points": "3",
    # Never finalize a week that started before this key (see module docstring).
    "league_first_week": _current_week_key(),
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
        raise RuntimeError(f"Unsupported group league config seed dialect: {dialect}")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table(_WEEKS):
        op.create_table(
            _WEEKS,
            sa.Column("week_key", sa.String(length=10), nullable=False),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("winner_group_id", sa.BigInteger(), nullable=True),
            sa.Column("winner_points", sa.Integer(), nullable=True),
            sa.Column(
                "apples_paid",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("week_key"),
        )

    if not _has_table(_AWARDS):
        op.create_table(
            _AWARDS,
            sa.Column(
                "id", sa.BigInteger(), autoincrement=True, nullable=False
            ),
            sa.Column("week_key", sa.String(length=10), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("group_id", sa.BigInteger(), nullable=False),
            sa.Column("points", sa.Integer(), nullable=False),
            sa.Column("wins", sa.Integer(), nullable=False),
            sa.Column("draws", sa.Integer(), nullable=False),
            sa.Column("losses", sa.Integer(), nullable=False),
            sa.Column("matches", sa.Integer(), nullable=False),
            sa.Column(
                "players_paid", sa.Integer(), server_default="0", nullable=False
            ),
            sa.Column(
                "apples_per_player", sa.Integer(), server_default="0", nullable=False
            ),
            sa.ForeignKeyConstraint(
                ["week_key"],
                [f"{_WEEKS}.week_key"],
                name="fk_group_league_awards_week",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["group_id"],
                ["groups.id"],
                name="fk_group_league_awards_group",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "week_key", "rank", name="uq_group_league_award_rank"
            ),
        )
        op.create_index(
            "ix_group_league_awards_week_key", _AWARDS, ["week_key"]
        )
        op.create_index(
            "ix_group_league_awards_group_id", _AWARDS, ["group_id"]
        )

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    # ``league_first_week`` is seeded with the migration's own week, so it is
    # matched exactly; the rest only when still untouched.
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text(
                "DELETE FROM system_config WHERE `key` = :key AND `value` = :value"
            ).bindparams(key=key, value=value)
        )
    if _has_table(_AWARDS):
        op.drop_table(_AWARDS)
    if _has_table(_WEEKS):
        op.drop_table(_WEEKS)
