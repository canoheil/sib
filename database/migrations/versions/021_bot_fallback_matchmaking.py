"""Persist bot identities, queue presentation refs, and separate bot records.

Revision ID: 021_bot_fallback_matchmaking
Revises: 020_factions
Create Date: 2026-09-30

The match already has an ``is_bot_match`` discriminator, so no match-type
column is needed. Bot profiles belong to individual match participants;
queue message ids let the timeout transition update the actual live panel;
and bot counters stay separate from competitive player statistics. Finished
legacy bot results are moved out of the competitive player counters.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "021_bot_fallback_matchmaking"
down_revision: Union[str, None] = "020_factions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _move_legacy_bot_results(bind) -> None:
    """Reclassify finished legacy bot results using portable SQLAlchemy SQL."""
    match_players = sa.table(
        "match_players",
        sa.column("match_id", sa.BigInteger()),
        sa.column("user_id", sa.BigInteger()),
        sa.column("result", sa.String()),
    )
    matches = sa.table(
        "matches",
        sa.column("id", sa.BigInteger()),
        sa.column("is_bot_match", sa.Boolean()),
        sa.column("result_applied", sa.Boolean()),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.BigInteger()),
        sa.column("is_bot", sa.Boolean()),
    )
    player_stats = sa.table(
        "player_stats",
        sa.column("user_id", sa.BigInteger()),
        sa.column("matches_played", sa.Integer()),
        sa.column("wins", sa.Integer()),
        sa.column("losses", sa.Integer()),
        sa.column("draws", sa.Integer()),
        sa.column("bot_matches_played", sa.Integer()),
        sa.column("bot_wins", sa.Integer()),
        sa.column("bot_losses", sa.Integer()),
        sa.column("bot_draws", sa.Integer()),
    )

    rows = bind.execute(
        sa.select(
            match_players.c.user_id,
            users.c.is_bot,
            sa.func.count().label("match_count"),
            sa.func.sum(
                sa.case((match_players.c.result == "WIN", 1), else_=0)
            ).label("wins"),
            sa.func.sum(
                sa.case((match_players.c.result == "LOSS", 1), else_=0)
            ).label("losses"),
            sa.func.sum(
                sa.case((match_players.c.result == "DRAW", 1), else_=0)
            ).label("draws"),
        )
        .select_from(
            match_players.join(matches, matches.c.id == match_players.c.match_id)
            .join(users, users.c.id == match_players.c.user_id)
        )
        .where(
            matches.c.is_bot_match.is_(True),
            matches.c.result_applied.is_(True),
            match_players.c.result.is_not(None),
        )
        .group_by(match_players.c.user_id, users.c.is_bot)
    ).mappings()

    for row in rows:
        if bool(row["is_bot"]):
            # Bot entities have no player-facing PvP or bot-result profile.
            continue
        values = {}
        for name in ("matches_played", "wins", "losses", "draws"):
            count = row["match_count"] if name == "matches_played" else row[name]
            column = player_stats.c[name]
            value = sa.func.coalesce(column, 0)
            values[name] = sa.case(
                (value >= count, value - count),
                else_=0,
            )
        for name in ("matches_played", "wins", "losses", "draws"):
            bot_column = player_stats.c[
                "bot_matches_played" if name == "matches_played" else f"bot_{name}"
            ]
            count = row["match_count"] if name == "matches_played" else row[name]
            values[bot_column.key] = count

        bind.execute(
            sa.update(player_stats)
            .where(player_stats.c.user_id == row["user_id"])
            .values(**values)
        )


def upgrade() -> None:
    op.add_column("match_players", sa.Column("bot_profile", sa.Text(), nullable=True))
    op.add_column("matchmaking_queue", sa.Column("message_id", sa.BigInteger(), nullable=True))
    op.add_column("player_stats", sa.Column("bot_matches_played", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("player_stats", sa.Column("bot_wins", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("player_stats", sa.Column("bot_losses", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("player_stats", sa.Column("bot_draws", sa.Integer(), nullable=False, server_default="0"))

    # Before this revision, bot outcomes were included in the same PlayerStats
    # counters as PvP. Preserve history while moving finalized human results to
    # bot-only counters. Bot entities themselves do not receive profile stats.
    _move_legacy_bot_results(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    player_stats = sa.table(
        "player_stats",
        sa.column("user_id", sa.BigInteger()),
        sa.column("matches_played", sa.Integer()),
        sa.column("wins", sa.Integer()),
        sa.column("losses", sa.Integer()),
        sa.column("draws", sa.Integer()),
        sa.column("bot_matches_played", sa.Integer()),
        sa.column("bot_wins", sa.Integer()),
        sa.column("bot_losses", sa.Integer()),
        sa.column("bot_draws", sa.Integer()),
    )
    for counter in ("matches_played", "wins", "losses", "draws"):
        bot_counter = "bot_matches_played" if counter == "matches_played" else f"bot_{counter}"
        bind.execute(
            sa.update(player_stats).values(
                **{
                    counter: sa.func.coalesce(player_stats.c[counter], 0)
                    + sa.func.coalesce(player_stats.c[bot_counter], 0)
                }
            )
        )

    op.drop_column("player_stats", "bot_draws")
    op.drop_column("player_stats", "bot_losses")
    op.drop_column("player_stats", "bot_wins")
    op.drop_column("player_stats", "bot_matches_played")
    op.drop_column("matchmaking_queue", "message_id")
    op.drop_column("match_players", "bot_profile")
