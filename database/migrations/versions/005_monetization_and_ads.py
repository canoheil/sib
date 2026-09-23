"""Add ad_campaigns table and ad_campaign_id to matches

Revision ID: 005_monetization_and_ads
Revises: 004_gameplay_and_ratings
Create Date: 2026-09-18 18:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '005_monetization_and_ads'
down_revision: Union[str, None] = '004_gameplay_and_ratings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create ad_campaigns table
    op.create_table(
        'ad_campaigns',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('sponsor_name', sa.String(length=255), nullable=False),
        sa.Column('campaign_name', sa.String(length=255), nullable=False),
        sa.Column('image_url', sa.String(length=512), nullable=True),
        sa.Column('ad_text', sa.Text(), nullable=False),
        sa.Column('cta_label', sa.String(length=100), nullable=True),
        sa.Column('target_url', sa.String(length=512), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False),
        sa.Column('start_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('frequency_mode', sa.String(length=50), server_default='smart', nullable=False),
        sa.Column('max_impressions', sa.Integer(), nullable=True),
        sa.Column('impressions_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('clicks_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('target_game_id', sa.Integer(), nullable=True),
        sa.Column('target_group_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['target_game_id'], ['games.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['target_group_id'], ['groups.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ad_campaigns_is_active', 'ad_campaigns', ['is_active'])

    # 2. Add ad_campaign_id to matches
    op.add_column('matches', sa.Column('ad_campaign_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_matches_ad_campaign_id', 'matches', 'ad_campaigns', ['ad_campaign_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_matches_ad_campaign_id', 'matches', type_='foreignkey')
    op.drop_column('matches', 'ad_campaign_id')
    op.drop_index('ix_ad_campaigns_is_active', 'ad_campaigns')
    op.drop_table('ad_campaigns')
