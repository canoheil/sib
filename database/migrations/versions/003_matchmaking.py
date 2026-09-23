"""Add matchmaking_queue and match tracking columns

Revision ID: 003_matchmaking
Revises: 002_parties
Create Date: 2026-09-18 16:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '003_matchmaking'
down_revision: Union[str, None] = '002_parties'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add party and message columns to matches
    op.add_column('matches', sa.Column('party1_id', sa.BigInteger(), nullable=True))
    op.add_column('matches', sa.Column('party2_id', sa.BigInteger(), nullable=True))
    op.add_column('matches', sa.Column('group1_message_id', sa.BigInteger(), nullable=True))
    op.add_column('matches', sa.Column('group2_message_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key('fk_matches_party1', 'matches', 'parties', ['party1_id'], ['id'])
    op.create_foreign_key('fk_matches_party2', 'matches', 'parties', ['party2_id'], ['id'])

    # 2. Create matchmaking_queue table
    op.create_table(
        'matchmaking_queue',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('party_id', sa.BigInteger(), nullable=False),
        sa.Column('group_id', sa.BigInteger(), nullable=False),
        sa.Column('game_id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=20), nullable=False),
        sa.Column('group_rating', sa.Float(), nullable=False, server_default='1500.0'),
        sa.Column('entered_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['party_id'], ['parties.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['game_id'], ['games.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_matchmaking_queue_party_id', 'matchmaking_queue', ['party_id'], unique=True)
    op.create_index('ix_matchmaking_queue_group_id', 'matchmaking_queue', ['group_id'])
    op.create_index('ix_matchmaking_queue_game_id', 'matchmaking_queue', ['game_id'])
    op.create_index('ix_matchmaking_queue_mode', 'matchmaking_queue', ['mode'])
    op.create_index('ix_matchmaking_queue_is_active', 'matchmaking_queue', ['is_active'])


def downgrade() -> None:
    op.drop_table('matchmaking_queue')
    op.drop_constraint('fk_matches_party2', 'matches', type_='foreignkey')
    op.drop_constraint('fk_matches_party1', 'matches', type_='foreignkey')
    op.drop_column('matches', 'group2_message_id')
    op.drop_column('matches', 'group1_message_id')
    op.drop_column('matches', 'party2_id')
    op.drop_column('matches', 'party1_id')
