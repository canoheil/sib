"""Initial schema for Goje

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-09-18 15:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users table
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('first_name', sa.String(length=255), nullable=False),
        sa.Column('last_name', sa.String(length=255), nullable=True),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('language_code', sa.String(length=10), nullable=False, server_default='fa'),
        sa.Column('is_bot', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_users_username', 'users', ['username'])

    # Groups table
    op.create_table(
        'groups',
        sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    # Games table
    op.create_table(
        'games',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('name_fa', sa.String(length=100), nullable=False),
        sa.Column('description_fa', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('supports_solo', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('supports_2v2', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('supports_4v4', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('turn_timeout_seconds', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_games_code', 'games', ['code'], unique=True)

    # Matches table
    op.create_table(
        'matches',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('game_id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=20), nullable=False, server_default='solo'),
        sa.Column('state', sa.String(length=50), nullable=False, server_default='CREATED'),
        sa.Column('group1_id', sa.BigInteger(), nullable=True),
        sa.Column('group2_id', sa.BigInteger(), nullable=True),
        sa.Column('winner_group_id', sa.BigInteger(), nullable=True),
        sa.Column('is_draw', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('game_state', sa.Text(), nullable=True),
        sa.Column('current_turn_player_id', sa.BigInteger(), nullable=True),
        sa.Column('turn_deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('group1_accepted', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('group2_accepted', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('acceptance_deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.id']),
        sa.ForeignKeyConstraint(['group1_id'], ['groups.id']),
        sa.ForeignKeyConstraint(['group2_id'], ['groups.id']),
        sa.ForeignKeyConstraint(['winner_group_id'], ['groups.id']),
        sa.ForeignKeyConstraint(['current_turn_player_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_matches_game_id', 'matches', ['game_id'])
    op.create_index('ix_matches_state', 'matches', ['state'])
    op.create_index('ix_matches_group1_id', 'matches', ['group1_id'])
    op.create_index('ix_matches_group2_id', 'matches', ['group2_id'])

    # Match Players table
    op.create_table(
        'match_players',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('match_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('group_id', sa.BigInteger(), nullable=False),
        sa.Column('team', sa.Integer(), nullable=False),
        sa.Column('turn_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('has_accepted', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_match_players_match_id', 'match_players', ['match_id'])
    op.create_index('ix_match_players_user_id', 'match_players', ['user_id'])
    op.create_index('ix_match_players_group_id', 'match_players', ['group_id'])

    # Ratings table
    op.create_table(
        'ratings',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('group_id', sa.BigInteger(), nullable=False),
        sa.Column('rating', sa.Float(), nullable=False, server_default='1500.0'),
        sa.Column('matches_played', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('wins', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('losses', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('draws', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ratings_group_id', 'ratings', ['group_id'], unique=True)
    op.create_index('ix_ratings_rating', 'ratings', ['rating'])

    # Sponsor Channels table
    op.create_table(
        'sponsor_channels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('channel_id', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('invite_link', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('channel_type', sa.String(length=20), nullable=False, server_default='OFFICIAL'),
        sa.Column('is_required', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('start_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sponsor_channels_channel_id', 'sponsor_channels', ['channel_id'], unique=True)


def downgrade() -> None:
    op.drop_table('sponsor_channels')
    op.drop_table('ratings')
    op.drop_table('match_players')
    op.drop_table('matches')
    op.drop_table('games')
    op.drop_table('groups')
    op.drop_table('users')
