"""Add persistent Invasion, wave, and participant records.

Revision ID: 026_invasion_persistence
Revises: 025_player_xp_progression
Create Date: 2026-10-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "026_invasion_persistence"
down_revision: Union[str, None] = "025_player_xp_progression"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


_STATUS_CHECK = "status IN ('SCHEDULED', 'ACTIVE', 'FINISHED')"
_OUTCOME_CHECK = (
    "outcome IS NULL OR outcome IN "
    "('TOTAL_VICTORY', 'NARROW_DEFENSE', 'DEFEAT')"
)
_MAIN_CARD_SNAPSHOT_CHECK = (
    "(main_card_id_snapshot IS NULL "
    "AND character_id_snapshot IS NULL "
    "AND character_code_snapshot IS NULL "
    "AND character_name_fa_snapshot IS NULL "
    "AND character_name_en_snapshot IS NULL "
    "AND character_artwork_snapshot IS NULL "
    "AND card_level_snapshot IS NULL "
    "AND power_snapshot IS NULL "
    "AND speed_snapshot IS NULL "
    "AND intelligence_snapshot IS NULL "
    "AND character_balance_version_snapshot IS NULL) "
    "OR (main_card_id_snapshot IS NOT NULL "
    "AND character_id_snapshot IS NOT NULL "
    "AND character_code_snapshot IS NOT NULL "
    "AND character_name_fa_snapshot IS NOT NULL "
    "AND card_level_snapshot IS NOT NULL "
    "AND power_snapshot IS NOT NULL "
    "AND speed_snapshot IS NOT NULL "
    "AND intelligence_snapshot IS NOT NULL "
    "AND character_balance_version_snapshot IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "invasions",
        sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("occurrence_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="SCHEDULED",
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_transition_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_wave_sequence", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("announcement_message_id", sa.BigInteger(), nullable=True),
        sa.Column("target_region_code", sa.String(length=64), nullable=True),
        sa.Column(
            "target_region_name_snapshot", sa.String(length=160), nullable=True
        ),
        sa.Column("enemy_faction_code", sa.String(length=64), nullable=False),
        sa.Column("enemy_power", sa.Integer(), nullable=False),
        sa.Column("event_config_json", sa.Text(), nullable=False),
        sa.Column("event_spec_json", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("occurrence_key", name="uq_invasions_occurrence_key"),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_invasions_status"),
        sa.CheckConstraint(_OUTCOME_CHECK, name="ck_invasions_outcome"),
    )
    op.create_index(
        "ix_invasions_group_status", "invasions", ["group_id", "status"]
    )
    op.create_index(
        "ix_invasions_status_next_transition_at",
        "invasions",
        ["status", "next_transition_at"],
    )

    op.create_table(
        "invasion_waves",
        sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("wave_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="SCHEDULED",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_transition_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enemy_composition_json", sa.Text(), nullable=False),
        sa.Column(
            "contribution_total",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
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
        sa.ForeignKeyConstraint(
            ["invasion_id"], ["invasions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "invasion_id", "wave_sequence", name="uq_invasion_waves_invasion_sequence"
        ),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_invasion_waves_status"),
    )
    op.create_index(
        "ix_invasion_waves_status_next_transition_at",
        "invasion_waves",
        ["status", "next_transition_at"],
    )

    op.create_table(
        "invasion_participants",
        sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "contribution_total",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("main_card_id_snapshot", sa.BigInteger(), nullable=True),
        sa.Column("character_id_snapshot", sa.BigInteger(), nullable=True),
        sa.Column("character_code_snapshot", sa.String(length=64), nullable=True),
        sa.Column(
            "character_name_fa_snapshot", sa.String(length=120), nullable=True
        ),
        sa.Column(
            "character_name_en_snapshot", sa.String(length=120), nullable=True
        ),
        sa.Column(
            "character_artwork_snapshot", sa.String(length=255), nullable=True
        ),
        sa.Column("card_level_snapshot", sa.Integer(), nullable=True),
        sa.Column("power_snapshot", sa.Integer(), nullable=True),
        sa.Column("speed_snapshot", sa.Integer(), nullable=True),
        sa.Column("intelligence_snapshot", sa.Integer(), nullable=True),
        sa.Column(
            "character_balance_version_snapshot", sa.Integer(), nullable=True
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
        sa.ForeignKeyConstraint(
            ["invasion_id"], ["invasions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "invasion_id", "user_id", name="uq_invasion_participants_invasion_user"
        ),
        sa.CheckConstraint(
            _MAIN_CARD_SNAPSHOT_CHECK,
            name="ck_invasion_participants_main_card_snapshot_complete",
        ),
    )


def downgrade() -> None:
    op.drop_table("invasion_participants")
    op.drop_index(
        "ix_invasion_waves_status_next_transition_at", table_name="invasion_waves"
    )
    op.drop_table("invasion_waves")
    op.drop_index(
        "ix_invasions_status_next_transition_at", table_name="invasions"
    )
    op.drop_index("ix_invasions_group_status", table_name="invasions")
    op.drop_table("invasions")
