"""Halloween global boss event tables, feature flag, and pumpkin_seed sell price.

Revision ID: 057_halloween_event
Revises: 056_garden_wither_grace_hours
Create Date: 2026-10-22

HW1 — Six dedicated tables for the global Sibomortis event:
  * ``halloween_event_state`` — global boss HP, phase, 7-day window, Day-7 weakening.
  * ``halloween_attacks`` — immutable attack log, participation proof, leaderboard source.
  * ``halloween_reward_claims`` — idempotent milestone claims (75%, 50%, 25%, 0%).
  * ``halloween_seed_cooldowns`` — per-user cooldown state for Boss Menu seed claims.
  * ``halloween_loot_drops`` — random group chest drops.
  * ``halloween_loot_drop_claims`` — first-N winner claims per group chest.

Also seeds ``halloween_enabled = false`` without overwriting an operator value
and merges ``"pumpkin_seed": 15`` into ``system_config["item_sell_prices"]``
whenever that row is present (or seeds the canonical map cleanly when absent).
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "057_halloween_event"
down_revision: Union[str, None] = "056_garden_wither_grace_hours"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite's autoincrement requires INTEGER PRIMARY KEY; MariaDB keeps BIGINT.
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_PUMPKIN_SEED_SELL_PRICE = 15
_BASE_ITEM_SELL_PRICES = {
    "apple_gold": 50,
    "apple_diamond": 100,
    "apple_obsidian": 200,
    "wedding_ring": 500,
    "flower": 15,
    "pumpkin_seed": _PUMPKIN_SEED_SELL_PRICE,
}


def _merge_pumpkin_seed_sell_price(connection, *, reverse: bool = False) -> None:
    """Ensure ``pumpkin_seed: 15`` is present in ``system_config['item_sell_prices']``.

    Preserves all custom prices set by an operator on existing items and any
    custom ``pumpkin_seed`` price already stored. On downgrade, deletes only a
    ``pumpkin_seed`` price still equal to the default 15 (or removes the row if
    it equals the exact uncustomized default map).
    """
    raw = connection.execute(
        sa.text("SELECT `value` FROM system_config WHERE `key` = 'item_sell_prices'")
    ).scalar_one_or_none()

    if reverse:
        if raw is None:
            return
        prices = json.loads(raw)
        if not isinstance(prices, dict):
            return
        if prices == _BASE_ITEM_SELL_PRICES:
            connection.execute(
                sa.text("DELETE FROM system_config WHERE `key` = 'item_sell_prices'")
            )
            return
        if prices.get("pumpkin_seed") != _PUMPKIN_SEED_SELL_PRICE:
            return
        del prices["pumpkin_seed"]
        connection.execute(
            sa.text(
                "UPDATE system_config SET `value` = :value WHERE `key` = 'item_sell_prices'"
            ),
            {"value": json.dumps(prices, ensure_ascii=False)},
        )
        return

    if raw is None:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value)"
            ),
            {
                "key": "item_sell_prices",
                "value": json.dumps(_BASE_ITEM_SELL_PRICES, ensure_ascii=False),
            },
        )
        return

    prices = json.loads(raw)
    if not isinstance(prices, dict):
        raise ValueError(
            "item_sell_prices must be a JSON object; refusing to overwrite it"
        )
    if "pumpkin_seed" in prices:
        return
    prices["pumpkin_seed"] = _PUMPKIN_SEED_SELL_PRICE
    connection.execute(
        sa.text(
            "UPDATE system_config SET `value` = :value WHERE `key` = 'item_sell_prices'"
        ),
        {"value": json.dumps(prices, ensure_ascii=False)},
    )


def upgrade() -> None:
    op.create_table(
        "halloween_event_state",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("boss_hp", sa.BigInteger(), nullable=False),
        sa.Column("max_hp", sa.BigInteger(), nullable=False),
        sa.Column("current_phase", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_day_weakened", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("hp_before_weaken", sa.BigInteger(), nullable=True),
        sa.Column("weakened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("defeated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finisher_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "announced_milestones_mask", sa.Integer(), server_default="0", nullable=False
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
            ["finisher_user_id"],
            ["users.id"],
            name="fk_hw_state_finisher",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("max_hp > 0", name="ck_hw_state_max_hp_pos"),
        sa.CheckConstraint(
            "boss_hp >= 0 AND boss_hp <= max_hp", name="ck_hw_state_hp_bounds"
        ),
        sa.CheckConstraint(
            "current_phase BETWEEN 1 AND 5", name="ck_hw_state_phase"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_hw_state_active", "halloween_event_state", ["is_active"])

    op.create_table(
        "halloween_attacks",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("card_id", sa.BigInteger(), nullable=True),
        sa.Column("character_id", sa.BigInteger(), nullable=True),
        sa.Column("damage", sa.Integer(), nullable=False),
        sa.Column("is_crit", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("boss_hp_after", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["halloween_event_state.id"],
            name="fk_hw_attacks_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_hw_attacks_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_hw_attacks_group",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["player_cards.id"],
            name="fk_hw_attacks_card",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name="fk_hw_attacks_character",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("damage > 0", name="ck_hw_attacks_damage_pos"),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_hw_attacks_event_user", "halloween_attacks", ["event_id", "user_id"]
    )
    op.create_index(
        "ix_hw_attacks_created_at", "halloween_attacks", ["created_at"]
    )

    op.create_table(
        "halloween_reward_claims",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("milestone_pct", sa.Integer(), nullable=False),
        sa.Column("reward_summary_json", sa.Text(), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["halloween_event_state.id"],
            name="fk_hw_claims_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_hw_claims_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "event_id",
            "user_id",
            "milestone_pct",
            name="uq_hw_reward_claims_event_user_pct",
        ),
        sa.CheckConstraint(
            "milestone_pct IN (75, 50, 25, 0)", name="ck_hw_reward_claims_pct"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_hw_reward_claims_user", "halloween_reward_claims", ["user_id"]
    )

    op.create_table(
        "halloween_seed_cooldowns",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_claims", sa.Integer(), server_default="1", nullable=False),
        sa.Column("total_seeds", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("event_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["halloween_event_state.id"],
            name="fk_hw_seed_cd_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_hw_seed_cd_user",
            ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "halloween_loot_drops",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("variant_code", sa.String(32), nullable=False),
        sa.Column(
            "status", sa.String(16), server_default="PENDING", nullable=False
        ),
        sa.Column("max_claims", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "claimed_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["halloween_event_state.id"],
            name="fk_hw_drops_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_hw_drops_group",
            ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_hw_drops_status_expires",
        "halloween_loot_drops",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_hw_drops_group_created",
        "halloween_loot_drops",
        ["group_id", "created_at"],
    )

    op.create_table(
        "halloween_loot_drop_claims",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("drop_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("seeds_granted", sa.Integer(), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["drop_id"],
            ["halloween_loot_drops.id"],
            name="fk_hw_drop_claims_drop",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_hw_drop_claims_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "drop_id", "user_id", name="uq_hw_drop_claims_drop_user"
        ),
        sa.UniqueConstraint(
            "drop_id", "slot_no", name="uq_hw_drop_claims_drop_slot"
        ),
        sa.CheckConstraint(
            "seeds_granted BETWEEN 1 AND 3", name="ck_hw_drop_claims_seeds"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_hw_drop_claims_user", "halloween_loot_drop_claims", ["user_id"]
    )

    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('halloween_enabled', 'false') "
                "ON DUPLICATE KEY UPDATE `key` = `key`"
            )
        )
    elif dialect == "sqlite":
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('halloween_enabled', 'false') "
                "ON CONFLICT (`key`) DO NOTHING"
            )
        )
    else:
        raise RuntimeError(
            f"Unsupported halloween_enabled seed dialect: {dialect}"
        )

    _merge_pumpkin_seed_sell_price(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _merge_pumpkin_seed_sell_price(connection, reverse=True)

    op.drop_index("ix_hw_drop_claims_user", table_name="halloween_loot_drop_claims")
    op.drop_table("halloween_loot_drop_claims")
    op.drop_index("ix_hw_drops_group_created", table_name="halloween_loot_drops")
    op.drop_index("ix_hw_drops_status_expires", table_name="halloween_loot_drops")
    op.drop_table("halloween_loot_drops")
    op.drop_table("halloween_seed_cooldowns")
    op.drop_index("ix_hw_reward_claims_user", table_name="halloween_reward_claims")
    op.drop_table("halloween_reward_claims")
    op.drop_index("ix_hw_attacks_created_at", table_name="halloween_attacks")
    op.drop_index("ix_hw_attacks_event_user", table_name="halloween_attacks")
    op.drop_table("halloween_attacks")
    op.drop_index("ix_hw_state_active", table_name="halloween_event_state")
    op.drop_table("halloween_event_state")
