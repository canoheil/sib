"""User-to-user apple transfers: audit + pending tables, transfer config.

Revision ID: 062_transfers
Revises: 061_seedling_sell_price
Create Date: 2026-10-23

Phase 1 (data layer) of the transfer feature. Two NEW tables only; no existing
table or row is altered.

``transfer_log``
    Immutable audit row per COMPLETED transfer. ``item_type`` is one of
    ``apple`` (the normal-Apple wallet CURRENCY), ``apple_gold``,
    ``apple_diamond`` or ``apple_obsidian`` (real ``user_items`` inventory
    keys). ``amount`` is what the receiver got, ``fee`` what the sender paid on
    top of it. Indexed by ``(from_user_id, created_at)`` for the cooldown and
    the Tehran-day daily limit, and by ``(to_user_id, created_at)`` for
    receiver history.

``pending_transfer``
    The two-step confirmation hand-off used in Phase 2: ``pending`` rows
    expire, the confirmation message stores ``chat_id``/``message_id``, and the
    state machine is pending -> processing -> done/failed/cancelled. Indexed
    by ``(state, expires_at)`` for the expiry sweep and by
    ``(from_user_id, state)`` for duplicate-pending checks.

It also seeds the six ``transfer_*`` config keys. Like every earlier config
seed (036, 046, 055, 060, 061) the insert never overwrites an existing
(possibly customized) value, and ``downgrade`` only removes the rows that
still hold the untouched default.

The revision is written to be re-runnable: ``upgrade`` checks for the tables
before creating them (``alembic upgrade head`` twice is a no-op apart from the
idempotent config inserts), and ``downgrade`` drops whatever exists.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "062_transfers"
down_revision: Union[str, None] = "061_seedling_sell_price"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite's autoincrement requires INTEGER PRIMARY KEY; MariaDB keeps BIGINT.
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# key -> seeded default (system_config values are strings; ConfigService
# decodes them as JSON, so "0"/"10000" become ints and "true" a bool).
_CONFIG_DEFAULTS: dict[str, str] = {
    "transfer_fee_percent": "0",
    "transfer_min_amount": "1",
    "transfer_max_amount": "10000",
    "transfer_daily_limit": "0",
    "transfer_cooldown_seconds": "0",
    "transfer_enabled": "true",
}

# Index names, kept in sync with database/models/transfer.py.
_TLOG_FROM_INDEX = "ix_transfer_log_from_user_id"
_TLOG_TO_INDEX = "ix_transfer_log_to_user_id"
_TLOG_FROM_CREATED_INDEX = "ix_transfer_log_from_user_created_at"
_TLOG_TO_CREATED_INDEX = "ix_transfer_log_to_user_created_at"
_PENDING_STATE_EXPIRES_INDEX = "ix_pending_transfer_state_expires_at"
_PENDING_FROM_STATE_INDEX = "ix_pending_transfer_from_user_state"


def _seed(key: str, value: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) "
                "ON DUPLICATE KEY UPDATE `key` = `key`"
            ).bindparams(key=key, value=value)
        )
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "(:key, :value) "
                "ON CONFLICT (`key`) DO NOTHING"
            ).bindparams(key=key, value=value)
        )
    else:
        raise RuntimeError(f"Unsupported transfer config seed dialect: {dialect}")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("transfer_log"):
        op.create_table(
            "transfer_log",
            sa.Column("id", _ID_TYPE, autoincrement=True, nullable=False),
            sa.Column("from_user_id", sa.BigInteger(), nullable=False),
            sa.Column("to_user_id", sa.BigInteger(), nullable=False),
            sa.Column("item_type", sa.String(length=32), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("fee", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["from_user_id"], ["users.id"], name="fk_transfer_log_from_user"
            ),
            sa.ForeignKeyConstraint(
                ["to_user_id"], ["users.id"], name="fk_transfer_log_to_user"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(_TLOG_FROM_INDEX, "transfer_log", ["from_user_id"])
        op.create_index(_TLOG_TO_INDEX, "transfer_log", ["to_user_id"])
        op.create_index(
            _TLOG_FROM_CREATED_INDEX, "transfer_log", ["from_user_id", "created_at"]
        )
        op.create_index(
            _TLOG_TO_CREATED_INDEX, "transfer_log", ["to_user_id", "created_at"]
        )

    if not _has_table("pending_transfer"):
        op.create_table(
            "pending_transfer",
            sa.Column("id", _ID_TYPE, autoincrement=True, nullable=False),
            sa.Column("from_user_id", sa.BigInteger(), nullable=False),
            sa.Column("to_user_id", sa.BigInteger(), nullable=False),
            sa.Column("item_type", sa.String(length=32), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "state",
                sa.String(length=16),
                server_default="pending",
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["from_user_id"], ["users.id"], name="fk_pending_transfer_from_user"
            ),
            sa.ForeignKeyConstraint(
                ["to_user_id"], ["users.id"], name="fk_pending_transfer_to_user"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            _PENDING_STATE_EXPIRES_INDEX, "pending_transfer", ["state", "expires_at"]
        )
        op.create_index(
            _PENDING_FROM_STATE_INDEX, "pending_transfer", ["from_user_id", "state"]
        )

    for key, value in _CONFIG_DEFAULTS.items():
        _seed(key, value)


def downgrade() -> None:
    if _has_table("pending_transfer"):
        op.drop_index(_PENDING_FROM_STATE_INDEX, table_name="pending_transfer")
        op.drop_index(_PENDING_STATE_EXPIRES_INDEX, table_name="pending_transfer")
        op.drop_table("pending_transfer")

    if _has_table("transfer_log"):
        op.drop_index(_TLOG_TO_CREATED_INDEX, table_name="transfer_log")
        op.drop_index(_TLOG_FROM_CREATED_INDEX, table_name="transfer_log")
        op.drop_index(_TLOG_TO_INDEX, table_name="transfer_log")
        op.drop_index(_TLOG_FROM_INDEX, table_name="transfer_log")
        op.drop_table("transfer_log")

    # Remove only the rows this migration seeded, i.e. while they still hold
    # the untouched default. A customized admin value is left in place.
    for key, value in _CONFIG_DEFAULTS.items():
        op.execute(
            sa.text(
                "DELETE FROM system_config WHERE `key` = :key AND `value` = :value"
            ).bindparams(key=key, value=value)
        )
