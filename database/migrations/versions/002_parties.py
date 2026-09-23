"""Add parties and party_members tables

Revision ID: 002_parties
Revises: 001_initial_schema
Create Date: 2026-09-18 16:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '002_parties'
down_revision: Union[str, None] = '001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'parties',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('group_id', sa.BigInteger(), nullable=False),
        sa.Column('game_id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=20), server_default='solo', nullable=False),
        sa.Column('max_players', sa.Integer(), server_default='1', nullable=False),
        sa.Column('current_players_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='OPEN', nullable=False),
        sa.Column('creator_user_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['creator_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['game_id'], ['games.id']),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_parties_group_id', 'parties', ['group_id'])
    op.create_index('ix_parties_game_id', 'parties', ['game_id'])
    op.create_index('ix_parties_status', 'parties', ['status'])

    op.create_table(
        'party_members',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('party_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('joined_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['party_id'], ['parties.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('party_id', 'user_id', name='uq_party_member'),
    )
    op.create_index('ix_party_members_party_id', 'party_members', ['party_id'])
    op.create_index('ix_party_members_user_id', 'party_members', ['user_id'])


def downgrade() -> None:
    op.drop_table('party_members')
    op.drop_table('parties')
