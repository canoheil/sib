"""Archived on 2026-10-14. Drop only after 2027-01-01.

Preserve every V1 row by renaming its table, including child tables. Historical
migrations remain unchanged. MariaDB updates foreign-key references on RENAME;
SQLite does so with foreign_keys enabled. Re-entry after partial DDL is safe.

Revision ID: 039_archive_inv1_tables
Revises: 038_inv2_admin_logs
"""

from alembic import op
import sqlalchemy as sa

revision = "039_archive_inv1_tables"
down_revision = "038_inv2_admin_logs"
branch_labels = None
depends_on = None

_TABLES = (
    ("invasions", "_archive_inv1_invasions"),
    ("invasion_waves", "_archive_inv1_waves"),
    ("invasion_contributions", "_archive_inv1_contributions"),
    ("invasion_defense_actions", "_archive_inv1_defense_actions"),
    ("invasion_participants", "_archive_inv1_participants"),
)


def _rename_if_exists(source: str, target: str) -> None:
    # Inspect anew after each DDL: MariaDB commits each RENAME independently.
    inspector = sa.inspect(op.get_bind())
    old, new = inspector.has_table(source), inspector.has_table(target)
    if old and new:
        raise RuntimeError(f"Both {source} and {target} exist; refusing to overwrite archived data")
    if old:
        op.rename_table(source, target)


def _has_delivered_at() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("inv2_invasions"):
        raise RuntimeError("inv2_invasions must exist before migration 039")
    return any(col["name"] == "delivered_at" for col in inspector.get_columns("inv2_invasions"))


def upgrade() -> None:
    for source, target in _TABLES:
        _rename_if_exists(source, target)
    if not _has_delivered_at():
        op.add_column("inv2_invasions", sa.Column("delivered_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Unwind the added column before returning to the old application schema.
    if _has_delivered_at():
        op.drop_column("inv2_invasions", "delivered_at")
    for source, target in reversed(_TABLES):
        _rename_if_exists(target, source)
