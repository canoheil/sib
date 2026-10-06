"""Add persistent Account XP progression and idempotent source ledger.

Revision ID: 025_player_xp_progression
Revises: 024_marriage_and_love
Create Date: 2026-10-06
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025_player_xp_progression"
down_revision: Union[str, None] = "024_marriage_and_love"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    if "player_progressions" not in existing_tables:
        op.create_table(
            "player_progressions",
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
                autoincrement=False,
            ),
            sa.Column(
                "total_xp",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "total_xp >= 0 AND total_xp <= 87000",
                name="ck_player_progressions_total_xp",
            ),
        )

    if "player_xp_grants" not in existing_tables:
        op.create_table(
            "player_xp_grants",
            sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("event_key", sa.String(length=255), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column(
                "awarded_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("level_before", sa.Integer(), nullable=False),
            sa.Column("level_after", sa.Integer(), nullable=False),
            sa.Column("notification_chat_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "notification_claimed_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "notification_sent_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "user_id", "source", "event_key", name="uq_player_xp_grant_event"
            ),
            sa.CheckConstraint(
                "amount >= 0", name="ck_player_xp_grants_amount_nonnegative"
            ),
            sa.CheckConstraint(
                "level_before >= 1 AND level_before <= 30 "
                "AND level_after >= 1 AND level_after <= 30",
                name="ck_player_xp_grants_levels",
            ),
        )
        op.create_index(
            "ix_player_xp_grants_user_awarded_at",
            "player_xp_grants",
            ["user_id", "awarded_at"],
        )
        op.create_index(
            "ix_player_xp_grants_user_source_awarded_at",
            "player_xp_grants",
            ["user_id", "source", "awarded_at"],
        )
        op.create_index(
            "ix_player_xp_grants_notification_pending",
            "player_xp_grants",
            ["notification_sent_at", "notification_claimed_at"],
        )

    # Completed historical matches must not be replayed as post-launch XP.
    # New matches default to unprocessed, and a completed result is marked
    # processed in its transaction unless a retryable XP-side failure occurs.
    if "matches" in existing_tables:
        match_columns = {
            column["name"] for column in sa.inspect(bind).get_columns("matches")
        }
        if "xp_awarded" not in match_columns:
            op.add_column(
                "matches",
                sa.Column(
                    "xp_awarded",
                    sa.Boolean(),
                    server_default=sa.text("0"),
                    nullable=False,
                ),
            )
            bind.execute(
                sa.text(
                    "UPDATE matches SET xp_awarded = 1 WHERE result_applied = 1"
                )
            )

    # Every pre-existing account starts at Level 1 / 0 XP. Never infer XP
    # from historical activity, inventory, currency, or relationship state.
    if "users" in existing_tables:
        bind.execute(
            sa.text(
                "INSERT INTO player_progressions "
                "(user_id, total_xp, created_at, updated_at) "
                "SELECT id, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM users "
                "WHERE id NOT IN (SELECT user_id FROM player_progressions)"
            )
        )

    # Mark existing user/character pairs as already collected, without
    # retroactive XP. This keeps the post-rollout collection milestone truly
    # one-time even for cards owned before this migration.
    now = datetime.now(timezone.utc)
    grant_table = sa.table(
        "player_xp_grants",
        sa.column("user_id", sa.BigInteger()),
        sa.column("source", sa.String(length=40)),
        sa.column("event_key", sa.String(length=255)),
        sa.column("amount", sa.Integer()),
        sa.column("awarded_at", sa.DateTime(timezone=True)),
        sa.column("level_before", sa.Integer()),
        sa.column("level_after", sa.Integer()),
    )
    if "player_cards" in existing_tables:
        existing_pairs = bind.execute(
            sa.text(
                "SELECT DISTINCT pc.user_id, pc.character_id "
                "FROM player_cards AS pc "
                "JOIN users AS u ON u.id = pc.user_id"
            )
        ).all()
        if existing_pairs:
            op.bulk_insert(
                grant_table,
                [
                    {
                        "user_id": int(user_id),
                        "source": "card_collection",
                        "event_key": f"card_collection:{int(character_id)}",
                        "amount": 0,
                        "awarded_at": now,
                        "level_before": 1,
                        "level_after": 1,
                    }
                    for user_id, character_id in existing_pairs
                ],
            )

    # Retain one-time garden milestones without converting historical state
    # to XP. The shipped defaults are five free plots, but unlock markers are
    # derived from the wallet's auditable successful-spend keys rather than
    # inferred from plot indices; this remains correct if an admin changed
    # garden_initial_plots before rollout. Planted plots and magic levels are
    # marked by stable garden/plot-slot identity.
    garden_markers = []
    if "garden_plots" in existing_tables:
        plot_rows = bind.execute(
            sa.text(
                "SELECT gp.id, gp.garden_id, gp.plot_index, gp.planted_at, "
                "gp.magic_level FROM garden_plots AS gp "
                "JOIN users AS u ON u.id = gp.garden_id"
            )
        ).all()
        for plot_id, garden_id, plot_index, planted_at, magic_level in plot_rows:
            if planted_at is not None:
                garden_markers.append(
                    {
                        "user_id": int(garden_id),
                        "source": "garden_plant",
                        "event_key": f"garden_plant:{int(garden_id)}:{int(plot_index)}",
                        "amount": 0,
                        "awarded_at": now,
                        "level_before": 1,
                        "level_after": 1,
                    }
                )
            for target_level in range(1, max(0, int(magic_level or 0)) + 1):
                garden_markers.append(
                    {
                        "user_id": int(garden_id),
                        "source": "garden_magic",
                        "event_key": (
                            f"garden_magic:{int(garden_id)}:{int(plot_index)}:"
                            f"{target_level}"
                        ),
                        "amount": 0,
                        "awarded_at": now,
                        "level_before": 1,
                        "level_after": 1,
                    }
                )
    # A successful plot unlock writes this exact wallet key in the same
    # transaction as adding the plot. It is the authoritative evidence of a
    # paid unlock and avoids guessing from a potentially changed config value.
    if "apple_transactions" in existing_tables:
        unlock_rows = bind.execute(
            sa.text(
                "SELECT user_id, idempotency_key FROM apple_transactions "
                "WHERE reason = 'garden_plot_unlock' "
                "AND idempotency_key IS NOT NULL"
            )
        ).all()
        for user_id, idempotency_key in unlock_rows:
            user_id = int(user_id)
            prefix = f"garden_unlock:{user_id}:"
            if not str(idempotency_key).startswith(prefix):
                continue
            try:
                plot_index = int(str(idempotency_key)[len(prefix):])
            except ValueError:
                continue
            if plot_index < 0:
                continue
            garden_markers.append(
                {
                    "user_id": user_id,
                    "source": "garden_unlock",
                    "event_key": f"garden_unlock:{user_id}:{plot_index}",
                    "amount": 0,
                    "awarded_at": now,
                    "level_before": 1,
                    "level_after": 1,
                }
            )

    if garden_markers:
        op.bulk_insert(grant_table, garden_markers)

    # An account with any pre-launch marriage record (active or divorced) is
    # ineligible for the post-launch first-marriage milestone. A zero-XP
    # idempotency marker records that exclusion; no historical XP is granted.
    if "marriages" in existing_tables:
        married_user_ids = bind.execute(
            sa.text(
                "SELECT user1_id FROM marriages "
                "UNION SELECT user2_id FROM marriages"
            )
        ).scalars().all()
        if married_user_ids:
            op.bulk_insert(
                grant_table,
                [
                    {
                        "user_id": int(user_id),
                        "source": "first_marriage",
                        "event_key": "first_marriage",
                        "amount": 0,
                        "awarded_at": now,
                        "level_before": 1,
                        "level_after": 1,
                    }
                    for user_id in married_user_ids
                ],
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "matches" in existing_tables:
        match_columns = {
            column["name"] for column in sa.inspect(bind).get_columns("matches")
        }
        if "xp_awarded" in match_columns:
            op.drop_column("matches", "xp_awarded")
    if "player_xp_grants" in existing_tables:
        op.drop_table("player_xp_grants")
    if "player_progressions" in existing_tables:
        op.drop_table("player_progressions")
