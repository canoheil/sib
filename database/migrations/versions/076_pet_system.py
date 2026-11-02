"""Add adoptable pet companions and seed pet-system configuration.

Revision ID: 076_pet_system
Revises: 075_rocket_game
"""

import sqlalchemy as sa
from alembic import op

revision = "076_pet_system"
down_revision = "075_rocket_game"
branch_labels = None
depends_on = None

_TABLE = "user_pets"
_USER_INDEX = "ix_user_pets_user_id"
# SQLite only autoincrements INTEGER PRIMARY KEY; MariaDB keeps BIGINT AUTO_INCREMENT.
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_CONFIG_DEFAULTS = {
    "pet_hunt_cooldown_hours": "2",
    "pet_feed_xp_amount": "50",
    "pet_adult_xp_required": "500",
    "pet_skill_max_level": "3",
    "pet_skill_upgrade_diamond_cost": "10",
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
        raise RuntimeError(f"Unsupported pet config seed dialect: {dialect}")
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
            sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("pet_type", sa.String(length=32), nullable=False),
            sa.Column("custom_name", sa.String(length=64), nullable=True),
            sa.Column(
                "is_adult", sa.Boolean(), server_default=sa.false(), nullable=False
            ),
            sa.Column("xp", sa.Integer(), server_default="0", nullable=False),
            sa.Column("skill_level", sa.Integer(), server_default="1", nullable=False),
            sa.Column("last_fed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_hunted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(_USER_INDEX, _TABLE, ["user_id"])

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    if _has_table():
        op.drop_index(_USER_INDEX, table_name=_TABLE)
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
        raise RuntimeError(f"Unsupported pet config seed dialect: {dialect}")
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text("DELETE FROM system_config WHERE " + predicate).bindparams(
                key=key, value=value
            )
        )
