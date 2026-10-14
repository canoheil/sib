"""Create independent Invasion v2 tables and seed regions/config defaults.

Revision ID: 036_invasion_v2
Revises: 035_apple_duel

No legacy invasion table or row is altered. Existing system_config values,
including invasion_enabled, are preserved by the insert-if-missing upsert.
"""

import sqlalchemy as sa
from alembic import op

revision = "036_invasion_v2"
down_revision = "035_apple_duel"
branch_labels = None
depends_on = None

# SQLite's autoincrement requires INTEGER PRIMARY KEY; MariaDB keeps BIGINT.
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "inv2_regions",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("name_fa", sa.String(160), nullable=False),
        sa.Column("controlling_faction", sa.String(64), nullable=False),
        sa.Column("is_lost_to_enemy", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_invasion_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "inv2_invasions",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("region_code", sa.String(64), nullable=False),
        sa.Column("enemy_type", sa.String(32), nullable=False),
        sa.Column("enemy_name_fa", sa.String(160), nullable=False),
        sa.Column("enemy_hp", sa.Integer(), nullable=False),
        sa.Column("enemy_hp_remaining", sa.Integer(), nullable=False),
        sa.Column("difficulty_multiplier", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("message_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("announcement_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrence_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["region_code"], ["inv2_regions.code"], name="fk_inv2_invasions_region"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_inv2_invasions_group"),
        sa.UniqueConstraint("occurrence_key", name="uq_inv2_invasions_occurrence_key"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_invasions_status_ends_at", "inv2_invasions", ["status", "ends_at"])
    op.create_index("ix_inv2_invasions_group_status", "inv2_invasions", ["group_id", "status"])
    op.create_index("ix_inv2_invasions_region_code", "inv2_invasions", ["region_code"])

    op.create_table(
        "inv2_attacks",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("card_id", sa.BigInteger(), nullable=False),
        sa.Column("card_power", sa.Integer(), nullable=False),
        sa.Column("damage", sa.Integer(), nullable=False),
        sa.Column("is_crit", sa.Boolean(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invasion_id"], ["inv2_invasions.id"], name="fk_inv2_attacks_invasion"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_inv2_attacks_user"),
        sa.ForeignKeyConstraint(["card_id"], ["player_cards.id"], name="fk_inv2_attacks_card"),
        sa.UniqueConstraint("invasion_id", "user_id", name="uq_inv2_attacks_invasion_user"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_attacks_user_id", "inv2_attacks", ["user_id"])
    op.create_index("ix_inv2_attacks_card_id", "inv2_attacks", ["card_id"])

    op.create_table(
        "inv2_rewards",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reward_type", sa.String(20), nullable=False),
        sa.Column("reward_amount", sa.Integer(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invasion_id"], ["inv2_invasions.id"], name="fk_inv2_rewards_invasion"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_inv2_rewards_user"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_rewards_invasion_id", "inv2_rewards", ["invasion_id"])
    op.create_index("ix_inv2_rewards_user_id", "inv2_rewards", ["user_id"])

    op.create_table(
        "inv2_group_state",
        sa.Column("group_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_victories", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_defeats", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_inv2_group_state_group"),
        mysql_charset="utf8mb4",
    )

    # Frozen UTF-8 seed data from docs/invasion-rework/PHASE-1-DESIGN.md.
    op.execute(sa.text("""
        INSERT INTO inv2_regions (code, name_fa, controlling_faction) VALUES
          ('moonfall_valley', 'درّه ماه', 'فرزندان ماه'),
          ('emerald_forest', 'جنگل زمرد', 'فرزانگان'),
          ('sunfield_plains', 'دشت آفتاب', 'امپراتوری'),
          ('diamond_peaks', 'کوهستان الماس', 'امپراتوری'),
          ('lotus_lake', 'دریاچه نیلوفر', 'فرزندان ماه'),
          ('ash_desert', 'بیابان خاکستر', 'بی‌طرف'),
          ('mirror_city', 'شهر آینه', 'امپراتوری'),
          ('whispering_caves', 'غار زمزمه‌ها', 'بی‌طرف'),
          ('star_harbor', 'بندر ستاره', 'امپراتوری'),
          ('dark_marsh', 'تالاب تاریک', 'فرزندان ماه')
    """))

    # system_config stores JSON scalar text. A no-op on duplicate leaves
    # existing values unchanged, especially a pre-existing disabled flag.
    values = """
        ('invasion_enabled', 'false'),
        ('invasion_join_window_seconds', '300'),
        ('invasion_interval_seconds', '3600'),
        ('invasion_difficulty_multiplier', '4.0'),
        ('invasion_max_active_per_group', '1'),
        ('invasion_circuit_breaker_threshold', '3'),
        ('invasion_mute_hours', '24')
    """
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES " + values
            + " ON DUPLICATE KEY UPDATE `key` = `key`"
        ))
    elif dialect == "sqlite":  # isolated test DB; MariaDB uses the upsert above
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES " + values
            + " ON CONFLICT (`key`) DO NOTHING"
        ))
    else:
        raise RuntimeError(f"Unsupported Invasion v2 seed dialect: {dialect}")


def downgrade() -> None:
    # Keep config keys: they may have existed before this migration or may
    # have been edited later. Without provenance, deletion would lose data.
    op.drop_table("inv2_group_state")
    op.drop_index("ix_inv2_rewards_user_id", table_name="inv2_rewards")
    op.drop_index("ix_inv2_rewards_invasion_id", table_name="inv2_rewards")
    op.drop_table("inv2_rewards")
    op.drop_index("ix_inv2_attacks_card_id", table_name="inv2_attacks")
    op.drop_index("ix_inv2_attacks_user_id", table_name="inv2_attacks")
    op.drop_table("inv2_attacks")
    op.drop_index("ix_inv2_invasions_region_code", table_name="inv2_invasions")
    op.drop_index("ix_inv2_invasions_group_status", table_name="inv2_invasions")
    op.drop_index("ix_inv2_invasions_status_ends_at", table_name="inv2_invasions")
    op.drop_table("inv2_invasions")
    op.drop_table("inv2_regions")
