"""Random events inside an active Invasion v2 battle.

Revision ID: 055_inv2_random_events
Revises: 054_garden_corruption
Create Date: 2026-10-20

IV3 — three independent tables; no existing table or row is altered.

* ``inv2_random_events`` — the per-invasion plan and lifecycle. The unique
  ``(invasion_id, slot_no)`` pair makes overlapping maintenance workers
  produce exactly one plan.
* ``inv2_event_effects`` — timed modifiers read by the join path, plus the
  one-shot HP effects kept as an audit trail.
* ``inv2_event_claims`` — one row per loot winner; ``(event_id, user_id)``
  and ``(event_id, slot_no)`` are the database-level guards against a
  duplicate click and a fourth winner.

``inv2_events_enabled`` is seeded **off** so a deploy cannot start firing
events before an operator enables them. Like every earlier invasion seed, the
insert never overwrites an existing (possibly customized) value.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "055_inv2_random_events"
down_revision: Union[str, None] = "054_garden_corruption"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite's autoincrement requires INTEGER PRIMARY KEY; MariaDB keeps BIGINT.
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "inv2_random_events",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("template_code", sa.String(48), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("applied_amount", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("loot_item_key", sa.String(32), nullable=True),
        sa.Column("loot_amount", sa.Integer(), nullable=True),
        sa.Column("loot_max_claims", sa.Integer(), nullable=True),
        sa.Column("loot_claimed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("send_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["invasion_id"], ["inv2_invasions.id"], name="fk_inv2_random_events_invasion"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_inv2_random_events_group"),
        sa.UniqueConstraint("invasion_id", "slot_no", name="uq_inv2_random_events_slot"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_random_events_invasion_status", "inv2_random_events", ["invasion_id", "status"])
    op.create_index("ix_inv2_random_events_status_scheduled", "inv2_random_events", ["status", "scheduled_at"])
    op.create_index("ix_inv2_random_events_group_id", "inv2_random_events", ["group_id"])

    op.create_table(
        "inv2_event_effects",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["inv2_random_events.id"], name="fk_inv2_event_effects_event"),
        sa.UniqueConstraint("event_id", "kind", name="uq_inv2_event_effects_event_kind"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_event_effects_event_id", "inv2_event_effects", ["event_id"])

    op.create_table(
        "inv2_event_claims",
        sa.Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("reward_type", sa.String(24), nullable=False),
        sa.Column("reward_amount", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["inv2_random_events.id"], name="fk_inv2_event_claims_event"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_inv2_event_claims_user"),
        sa.UniqueConstraint("event_id", "user_id", name="uq_inv2_event_claims_event_user"),
        sa.UniqueConstraint("event_id", "slot_no", name="uq_inv2_event_claims_event_slot"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inv2_event_claims_user_id", "inv2_event_claims", ["user_id"])

    # Operator-visible kill switch, seeded OFF. Never overwrite a value the
    # operator already set (including an explicit "true" from a hotfix).
    dialect = op.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES "
            "('inv2_events_enabled', 'false') "
            "ON DUPLICATE KEY UPDATE `key` = `key`"
        ))
    elif dialect == "sqlite":  # isolated test databases only
        op.execute(sa.text(
            "INSERT INTO system_config (`key`, `value`) VALUES "
            "('inv2_events_enabled', 'false') "
            "ON CONFLICT (`key`) DO NOTHING"
        ))
    else:
        raise RuntimeError(f"Unsupported inv2 random-events seed dialect: {dialect}")


def downgrade() -> None:
    op.drop_index("ix_inv2_event_claims_user_id", table_name="inv2_event_claims")
    op.drop_table("inv2_event_claims")
    op.drop_index("ix_inv2_event_effects_event_id", table_name="inv2_event_effects")
    op.drop_table("inv2_event_effects")
    op.drop_index("ix_inv2_random_events_group_id", table_name="inv2_random_events")
    op.drop_index("ix_inv2_random_events_status_scheduled", table_name="inv2_random_events")
    op.drop_index("ix_inv2_random_events_invasion_status", table_name="inv2_random_events")
    op.drop_table("inv2_random_events")
    # The config key is intentionally kept: it may predate this migration or
    # have been customized, and the code default ("disabled") is safe.
