"""Friendly parties ("بازی با اعضای گروه") as a MATCH TYPE, not a game mode.

Revision ID: 023_friendly_parties
Revises: 022_card_upgrade_levels
Create Date: 2026-10-01

The hierarchy stays Game -> Game Mode -> Match Type:

* ``parties.match_type``        : normal | friendly
* ``party_members.team``        : 1 | 2 (normal parties keep team 1)
* ``party_members.bot_profile`` : per-bot config for slots filled by the
  party bot-fill option (copied to ``match_players.bot_profile`` at start)
* ``matches.match_type``        : normal | friendly
* ``matches.winner_team``       : 1 | 2, needed because a friendly match has
  ``group1_id == group2_id`` so ``winner_group_id`` cannot name the winning
  side.

A friendly party holds BOTH teams of one group-internal match, so its
``max_players`` is twice the mode's team size (solo 2, 2v2 4, 4v4 8). Existing
rows default to ``normal``/team 1 and keep their exact current behavior.

Reversible: the downgrade only drops the new columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023_friendly_parties"
down_revision: Union[str, None] = "022_card_upgrade_levels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "parties",
        sa.Column("match_type", sa.String(length=20), nullable=False, server_default="normal"),
    )
    op.add_column(
        "party_members",
        sa.Column("team", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "party_members",
        sa.Column("bot_profile", sa.Text(), nullable=True),
    )
    op.add_column(
        "matches",
        sa.Column("match_type", sa.String(length=20), nullable=False, server_default="normal"),
    )
    op.add_column(
        "matches",
        sa.Column("winner_team", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("matches", "winner_team")
    op.drop_column("matches", "match_type")
    op.drop_column("party_members", "bot_profile")
    op.drop_column("party_members", "team")
    op.drop_column("parties", "match_type")
