"""Add Apple Rocket cooldown state and seed game configuration.

Revision ID: 075_rocket_game
Revises: 074_theft_system
"""

import sqlalchemy as sa
from alembic import op

revision = "075_rocket_game"
down_revision = "074_theft_system"
branch_labels = None
depends_on = None

_TABLE = "user_rocket_states"
_CONFIG_DEFAULTS = {
    "rocket_min_bet": "50",
    "rocket_max_bet": "5000",
    "rocket_cooldown_seconds": "30",
    "rocket_house_edge": "5",
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
        raise RuntimeError(f"Unsupported rocket config seed dialect: {dialect}")
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
            sa.Column(
                "last_played_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
        )

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    if _has_table():
        op.drop_table(_TABLE)

    # Do not remove an operator-edited value when rolling the feature back.
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        predicate = "BINARY `key` = BINARY :key AND BINARY `value` = BINARY :value"
    elif dialect == "sqlite":
        predicate = "`key` = :key COLLATE BINARY AND `value` = :value COLLATE BINARY"
    else:
        raise RuntimeError(f"Unsupported rocket config seed dialect: {dialect}")
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
                key=key, value=value
            )
        )
