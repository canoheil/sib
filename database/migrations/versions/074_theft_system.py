"""Add per-player wanted/theft state and seed its configuration.

Revision ID: 074_theft_system
Revises: 073_disease_started_flag
"""

import sqlalchemy as sa
from alembic import op

revision = "074_theft_system"
down_revision = "073_disease_started_flag"
branch_labels = None
depends_on = None

_TABLE = "user_wanted_states"
_CONFIG_DEFAULTS = {
    "theft_cooldown_minutes": "20",
    "theft_jail_hours": "2",
    "theft_amount_percent": "5",
    "theft_min_amount": "50",
    "theft_max_amount": "500",
    "theft_arrest_window_seconds": "10",
    "theft_arrest_reward_per_star": "100",
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
        raise RuntimeError(f"Unsupported theft config seed dialect: {dialect}")
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
            sa.Column("wanted_level", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_stolen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("wanted_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("jailed_until", sa.DateTime(timezone=True), nullable=True),
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
        raise RuntimeError(f"Unsupported theft config seed dialect: {dialect}")
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
                key=key, value=value
            )
        )
