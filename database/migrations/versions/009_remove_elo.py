"""Remove Elo/rating system; add win-based group stats.

Production data preservation:
- Existing per-group wins/losses/draws/matches_played are copied from the
  ``ratings`` table into ``group_stats`` before ``ratings`` is dropped.
- ``matches.rating_applied`` is renamed to ``result_applied`` so existing
  idempotency markers survive (results already applied stay applied).
- Obsolete Elo bookkeeping columns are dropped.

The upgrade is schema-inspected and therefore safe to re-run after an
interrupted attempt.

Revision ID: 009_remove_elo
Revises: 008_panel_owners
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_remove_elo"
down_revision: Union[str, None] = "008_panel_owners"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    match_cols = {c["name"] for c in inspector.get_columns("matches")}
    queue_cols = {c["name"] for c in inspector.get_columns("matchmaking_queue")}

    # 1. New win-based group stats table.
    if "group_stats" not in tables:
        op.create_table(
            "group_stats",
            sa.Column("group_id", sa.BigInteger(), nullable=False),
            sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("draws", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("matches_played", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("group_id"),
        )

    # 2. Preserve existing production statistics, then drop the Elo table.
    if "ratings" in tables:
        op.execute(
            """
            INSERT IGNORE INTO group_stats
                (group_id, wins, losses, draws, matches_played, created_at, updated_at)
            SELECT group_id, wins, losses, draws, matches_played, NOW(), NOW() FROM ratings
            """
        )
        op.drop_table("ratings")

    # 3. Matches: keep idempotency data, remove Elo change bookkeeping.
    if "rating_applied" in match_cols:
        op.alter_column(
            "matches",
            "rating_applied",
            new_column_name="result_applied",
            existing_type=sa.Boolean(),
        )
    if "group1_rating_change" in match_cols:
        op.drop_column("matches", "group1_rating_change")
    if "group2_rating_change" in match_cols:
        op.drop_column("matches", "group2_rating_change")

    # 4. Matchmaking queue: no skill rating snapshot anymore.
    if "group_rating" in queue_cols:
        op.drop_column("matchmaking_queue", "group_rating")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    match_cols = {c["name"] for c in inspector.get_columns("matches")}
    queue_cols = {c["name"] for c in inspector.get_columns("matchmaking_queue")}

    if "group_rating" not in queue_cols:
        op.add_column(
            "matchmaking_queue",
            sa.Column("group_rating", sa.Float(), nullable=False, server_default="1500.0"),
        )
    if "group2_rating_change" not in match_cols:
        op.add_column(
            "matches",
            sa.Column("group2_rating_change", sa.Float(), nullable=False, server_default="0.0"),
        )
    if "group1_rating_change" not in match_cols:
        op.add_column(
            "matches",
            sa.Column("group1_rating_change", sa.Float(), nullable=False, server_default="0.0"),
        )
    if "result_applied" in match_cols:
        op.alter_column(
            "matches",
            "result_applied",
            new_column_name="rating_applied",
            existing_type=sa.Boolean(),
        )

    if "ratings" not in tables:
        op.create_table(
            "ratings",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("group_id", sa.BigInteger(), nullable=False),
            sa.Column("rating", sa.Float(), nullable=False, server_default="1500.0"),
            sa.Column("matches_played", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("draws", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ratings_group_id", "ratings", ["group_id"], unique=True)
        op.create_index("ix_ratings_rating", "ratings", ["rating"])

        op.execute(
            """
            INSERT INTO ratings
                (group_id, rating, matches_played, wins, losses, draws, created_at, updated_at)
            SELECT group_id, 1500.0, matches_played, wins, losses, draws, NOW(), NOW() FROM group_stats
            """
        )
    if "group_stats" in tables:
        op.drop_table("group_stats")
