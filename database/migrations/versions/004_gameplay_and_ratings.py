"""Add rating tracking and player stats

Revision ID: 004_gameplay_and_ratings
Revises: 003_matchmaking
Create Date: 2026-09-18 17:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '004_gameplay_and_ratings'
down_revision: Union[str, None] = '003_matchmaking'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add rating applied and rating change columns to matches
    op.add_column('matches', sa.Column('rating_applied', sa.Boolean(), server_default='0', nullable=False))
    op.add_column('matches', sa.Column('group1_rating_change', sa.Float(), server_default='0.0', nullable=False))
    op.add_column('matches', sa.Column('group2_rating_change', sa.Float(), server_default='0.0', nullable=False))

    # 2. Add result column to match_players
    op.add_column('match_players', sa.Column('result', sa.String(length=10), nullable=True))

    # 3. Create player_stats table
    op.create_table(
        'player_stats',
        sa.Column('user_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('matches_played', sa.Integer(), server_default='0', nullable=False),
        sa.Column('wins', sa.Integer(), server_default='0', nullable=False),
        sa.Column('losses', sa.Integer(), server_default='0', nullable=False),
        sa.Column('draws', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade() -> None:
    op.drop_table('player_stats')
    op.drop_column('match_players', 'result')
    op.drop_column('matches', 'group2_rating_change')
    op.drop_column('matches', 'group1_rating_change')
    op.drop_column('matches', 'rating_applied')
