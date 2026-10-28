"""Add per-player disease state and seed its configuration.

Revision ID: 070_disease_system
Revises: 069_golden_hour_notices
"""

import sqlalchemy as sa
from alembic import op

revision = "070_disease_system"
down_revision = "069_golden_hour_notices"
branch_labels = None
depends_on = None

_TABLE = "user_diseases"
_CONFIG_DEFAULTS = {
    "disease_enabled": "true",
    "disease_immunity_hours": "24",
    "disease_initial_infection_percent": "15",
    "disease_daily_infection_chance": "10",
    "disease_mine_infection_chance": "2",
    "disease_duel_infection_chance": "5",
    "disease_demon_power_chance": "15",
    "disease_antidote_shop_price": "2000",
    "disease_craft_apple_cost": "200",
    "disease_doctor_daily_limit": "5",
    "disease_doctor_reward_apples": "50",
    "disease_doctor_infection_chance": "10",
}


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def _seed(key: str, value: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        conflict_clause = "ON DUPLICATE KEY UPDATE `key` = `key`"
    elif dialect == "sqlite":
        conflict_clause = "ON CONFLICT (`key`) DO NOTHING"
    else:
        raise RuntimeError(f"Unsupported disease config seed dialect: {dialect}")
    op.execute(
        sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES (:key, :value) "
            + conflict_clause
        ).bindparams(key=key, value=value)
    )


def upgrade() -> None:
    if not _has_table():
        op.create_table(
            _TABLE,
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
                autoincrement=False,
            ),
            sa.Column("infection_level", sa.Integer(), server_default="0", nullable=False),
            sa.Column("has_demon_power", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("infected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("immunity_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        )

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    if _has_table():
        op.drop_table(_TABLE)

    # Preserve operator overrides; as in earlier seeds, equality with the
    # original value is the available proxy for an untouched default. Compare
    # bytes rather than using a potentially case-insensitive database collation.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        predicate = "BINARY `key` = BINARY :key AND BINARY `value` = BINARY :value"
    elif dialect == "sqlite":
        predicate = "`key` = :key COLLATE BINARY AND `value` = :value COLLATE BINARY"
    else:
        raise RuntimeError(f"Unsupported disease config seed dialect: {dialect}")
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
                key=key, value=value
            )
        )
