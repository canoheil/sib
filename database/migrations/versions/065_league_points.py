"""Multi-source weekly league: three covering indexes and the scoring table.

Revision ID: 065_league_points
Revises: 064_group_league
Create Date: 2026-10-26

The league used to score only group matches, so a group that spent the week
at war, mining and duelling was invisible. Phase 1 scores all four sources —
and every one of them is *derived* from a table that already exists. That
means **no new table and no new column**: the only schema work is the three
indexes the weekly queries need, so the panel stays one cheap read instead of
four table scans.

* ``apple_mine_shift (group_id, worked_at)`` — normal apples dug in a group;
* ``apple_duels (group_id, created_at)`` — who duelled inside a group;
* ``war_attacks (created_at)`` — damage each group dealt this week.

The scoring table itself is configuration, seeded insert-if-missing so an
operator's rebalanced ``league_points`` is never overwritten. The old
``league_min_points`` of 3 was tuned for match-only scoring; with four
sources it is raised to 15 (four match wins, or a single war) — but only for
installations that never changed it from the seeded 3.

Nothing here rewrites history: past weeks keep the frozen rows in
``group_league_awards`` / ``group_league_weeks``, and no match, war, shift or
duel row is touched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "065_league_points"
down_revision: Union[str, None] = "064_group_league"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (index name, table, columns)
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ix_apple_mine_shift_group_worked_at",
        "apple_mine_shift",
        ("group_id", "worked_at"),
    ),
    (
        "ix_apple_duels_group_created",
        "apple_duels",
        ("group_id", "created_at"),
    ),
    ("ix_war_attacks_created_at", "war_attacks", ("created_at",)),
)

#: The same values as ``services/group_league.py::DEFAULT_POINTS``.
_LEAGUE_POINTS = (
    '{"match_win": 3, "match_draw": 1, "match_cap": 45, '
    '"war_win": 10, "war_presence": 3, "war_damage_step": 100, "war_cap": 30, '
    '"mine_apples_step": 25, "mine_cap": 10, '
    '"duel_member": 1, "duel_cap": 5}'
)

#: The low floor the match-only league used; raised once, only if untouched.
_OLD_MIN_POINTS = "3"
_NEW_MIN_POINTS = "15"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table)
        if index.get("name")
    }


def _config_is(key: str, value: str) -> bool:
    row = op.get_bind().execute(
        sa.text("SELECT `value` FROM system_config WHERE `key` = :key").bindparams(
            key=key
        )
    ).first()
    return row is not None and row[0] == value


def _operators_changed_min_points() -> bool:
    """True when an operator set ``league_min_points`` to something not ours."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT `value` FROM system_config WHERE `key` = 'league_min_points'"
        )
    ).first()
    return row is not None and row[0] not in (_OLD_MIN_POINTS, _NEW_MIN_POINTS)


def _seed(key: str, value: str) -> None:
    """Insert a config row only when the key does not exist yet."""
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
        raise RuntimeError(f"Unsupported league points seed dialect: {dialect}")


def upgrade() -> None:
    tables = _tables()
    for name, table, columns in _INDEXES:
        if table not in tables or name in _index_names(table):
            continue
        op.create_index(name, table, list(columns))

    _seed("league_points", _LEAGUE_POINTS)

    if _config_is("league_min_points", _OLD_MIN_POINTS):
        op.execute(
            sa.text(
                "UPDATE system_config SET `value` = :new "
                "WHERE `key` = 'league_min_points' AND `value` = :old"
            ).bindparams(new=_NEW_MIN_POINTS, old=_OLD_MIN_POINTS)
        )


def downgrade() -> None:
    if _config_is("league_min_points", _NEW_MIN_POINTS):
        op.execute(
            sa.text(
                "UPDATE system_config SET `value` = :old "
                "WHERE `key` = 'league_min_points' AND `value` = :new"
            ).bindparams(new=_NEW_MIN_POINTS, old=_OLD_MIN_POINTS)
        )
    if _config_is("league_points", _LEAGUE_POINTS):
        op.execute(
            sa.text("DELETE FROM system_config WHERE `key` = 'league_points'")
        )

    tables = _tables()
    for name, table, _columns in _INDEXES:
        if table in tables and name in _index_names(table):
            op.drop_index(name, table_name=table)
