"""Add Friendly match metadata, team snapshots, reward claims, and trade DMs.

Revision ID: 023_friendly_match_foundation
Revises: 022_card_upgrade_levels
Create Date: 2026-09-30

Existing rows remain Competitive, existing party members remain on team 1,
and new per-match/per-user ledgers start empty. Friendly reward policy lives
in ConfigService defaults; this migration only adds durable state needed to
make claims and private trade selections safe and idempotent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "023_friendly_match_foundation"
down_revision: Union[str, None] = "022_card_upgrade_levels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Old parties and matches are explicitly Competitive; all existing party
    # members keep the historical one-side assignment (team 1).
    op.add_column(
        "parties",
        sa.Column("match_type", sa.String(length=20), nullable=False, server_default="COMPETITIVE"),
    )
    op.create_index("ix_parties_match_type", "parties", ["match_type"])

    op.add_column(
        "party_members",
        sa.Column("team", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("party_members", sa.Column("bot_profile", sa.Text(), nullable=True))
    op.add_column("party_members", sa.Column("selected_card_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_party_member_selected_card",
        "party_members",
        "player_cards",
        ["selected_card_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "matches",
        sa.Column("match_type", sa.String(length=20), nullable=False, server_default="COMPETITIVE"),
    )
    op.add_column("matches", sa.Column("winner_team", sa.Integer(), nullable=True))
    op.add_column("matches", sa.Column("rematch_of_match_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_matches_rematch_source",
        "matches",
        "matches",
        ["rematch_of_match_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_matches_rematch_of_match", "matches", ["rematch_of_match_id"]
    )
    op.create_index("ix_matches_match_type", "matches", ["match_type"])

    op.create_table(
        "friendly_match_rewards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reward_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], name="fk_friendly_reward_match", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_friendly_reward_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "user_id", name="uq_friendly_reward_match_user"),
    )
    op.create_index("ix_friendly_match_rewards_match_id", "friendly_match_rewards", ["match_id"])
    op.create_index("ix_friendly_match_rewards_user_id", "friendly_match_rewards", ["user_id"])
    op.create_index(
        "ix_friendly_reward_user_date",
        "friendly_match_rewards",
        ["user_id", "reward_date"],
    )

    op.create_table(
        "trade_selections",
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("proposer_id", sa.BigInteger(), nullable=False),
        sa.Column("proposer_card_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("offer_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("offer_message_id", sa.BigInteger(), nullable=True),
        sa.Column("recipient_card_id", sa.BigInteger(), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposer_id"], ["users.id"], name="fk_trade_selection_proposer", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["proposer_card_id"], ["player_cards.id"],
            name="fk_trade_selection_proposer_card", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["users.id"], name="fk_trade_selection_recipient", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_card_id"], ["player_cards.id"],
            name="fk_trade_selection_recipient_card", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name="fk_trade_selection_trade", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index("ix_trade_selections_proposer_id", "trade_selections", ["proposer_id"])
    op.create_index("ix_trade_selections_recipient_id", "trade_selections", ["recipient_id"])
    op.create_index(
        "ix_trade_selections_recipient_status", "trade_selections", ["recipient_id", "status"]
    )
    op.create_index("ix_trade_selections_expires_at", "trade_selections", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_trade_selections_expires_at", table_name="trade_selections")
    op.drop_index("ix_trade_selections_recipient_status", table_name="trade_selections")
    op.drop_index("ix_trade_selections_recipient_id", table_name="trade_selections")
    op.drop_index("ix_trade_selections_proposer_id", table_name="trade_selections")
    op.drop_table("trade_selections")

    op.drop_index("ix_friendly_reward_user_date", table_name="friendly_match_rewards")
    op.drop_index("ix_friendly_match_rewards_user_id", table_name="friendly_match_rewards")
    op.drop_index("ix_friendly_match_rewards_match_id", table_name="friendly_match_rewards")
    op.drop_table("friendly_match_rewards")

    op.drop_index("ix_matches_match_type", table_name="matches")
    op.drop_constraint("uq_matches_rematch_of_match", "matches", type_="unique")
    op.drop_constraint("fk_matches_rematch_source", "matches", type_="foreignkey")
    op.drop_column("matches", "rematch_of_match_id")
    op.drop_column("matches", "winner_team")
    op.drop_column("matches", "match_type")

    op.drop_constraint("fk_party_member_selected_card", "party_members", type_="foreignkey")
    op.drop_column("party_members", "selected_card_id")
    op.drop_column("party_members", "bot_profile")
    op.drop_column("party_members", "team")
    op.drop_index("ix_parties_match_type", table_name="parties")
    op.drop_column("parties", "match_type")
