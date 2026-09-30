"""Faction lore metadata for characters.

Revision ID: 020_factions
Revises: 019_magic_trees
Create Date: 2026-09-30

Additive, NON-DESTRUCTIVE change:

1. New ``factions`` table (code, Persian/English names, lore, territory,
   symbol, display order, active flag).
2. ``characters.faction_id`` (nullable FK) and ``characters.faction_role_fa``
   (nullable) - a character may stand outside every faction, so both stay
   nullable.
3. Data: the three lore factions are inserted when missing, and existing
   characters get their faction/role ONLY where those fields are still
   NULL. Hand-edited or already-migrated rows are never overwritten.

No column is dropped and no existing value is modified, so user data is
preserved. Factions are lore only: nothing in this migration touches
stats, balance versions or gameplay.

Note: the deprecated ``characters.rarity`` column from 012 is deliberately
left untouched (data preservation); it is simply no longer read by the
application.
"""

from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "020_factions"
down_revision: Union[str, None] = "019_magic_trees"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (code, name_fa, name_en, symbol, display_order, territory_fa, lore_fa)
_FACTIONS = (
    (
        "empire",
        "امپراتوری",
        "Empire",
        "👑",
        1,
        None,
        "قدرتمند، نظامی‌محور و مغرور؛ امپراتوری بر نظم و سلسله‌مراتب بنا شده است."
        " جنگجویانش خود را مستقل می‌دانند، اما در برابر تاج سر خم می‌کنند.",
    ),
    (
        "sages",
        "فرزانگان",
        "Sages",
        "🔮",
        2,
        None,
        "دانایانی قدرتمند اما منزوی؛ فرزانگان به‌جای زور، با تدبیر و دانش راه می‌روند"
        " و کمتر در میدان دیده می‌شوند. پادشاه فرزانگان بر آن‌ها فرمان می‌راند.",
    ),
    (
        "moon_descendants",
        "فرزندان ماه",
        "Moon Descendants",
        "🌙",
        3,
        "درّه ماه (Moonfall Valley)",
        "ساکنان درّه ماه، سرزمینی دور با قلعه‌ای کهن. «ملکه ماه» رهبر آن‌هاست و"
        " هدف واقعی این سازمان هنوز بر هیچ‌کس آشکار نیست.",
    ),
)

# Existing seeded characters -> faction code (only applied where NULL).
_CHARACTER_FACTIONS = {
    "sib_sorkh": "empire",
    "sib_sabz": "sages",
    "sib_talayi": "sages",
    "sib_mahi": "moon_descendants",
}

# Existing seeded characters -> role inside their faction (only applied
# where still empty).
_CHARACTER_ROLES = {
    "sib_sorkh": "قوی‌ترین جنگجوی امپراتوری",
    "sib_sabz": "فرزانگان استراتژیست و راست‌دست پادشاه فرزانگان؛ آموزگار سیب طلایی",
    "sib_talayi": "شاهزاده فرزانگان، دختر پادشاه فرزانگان",
    "sib_mahi": "ملکه ماه، رهبر فرزندان ماه",
}


def _factions_table() -> sa.Table:
    return sa.table(
        "factions",
        sa.column("code", sa.String),
        sa.column("name_fa", sa.String),
        sa.column("name_en", sa.String),
        sa.column("lore_fa", sa.Text),
        sa.column("territory_fa", sa.String),
        sa.column("symbol", sa.String),
        sa.column("display_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )


def _insert_missing_factions(conn) -> None:
    for code, name_fa, name_en, symbol, display_order, territory, lore in _FACTIONS:
        exists = conn.execute(
            sa.text("SELECT id FROM factions WHERE code = :code"), {"code": code}
        ).first()
        if exists:
            continue
        op.bulk_insert(
            _factions_table(),
            [
                {
                    "code": code,
                    "name_fa": name_fa,
                    "name_en": name_en,
                    "lore_fa": lore,
                    "territory_fa": territory,
                    "symbol": symbol,
                    "display_order": display_order,
                    "is_active": True,
                }
            ],
        )


def _backfill_characters(conn) -> None:
    for character_code, faction_code in _CHARACTER_FACTIONS.items():
        conn.execute(
            sa.text(
                "UPDATE characters SET faction_id = ("
                "SELECT id FROM factions WHERE code = :faction_code"
                ") WHERE code = :code AND faction_id IS NULL"
            ),
            {"faction_code": faction_code, "code": character_code},
        )
    for character_code, role in _CHARACTER_ROLES.items():
        conn.execute(
            sa.text(
                "UPDATE characters SET faction_role_fa = :role "
                "WHERE code = :code "
                "AND (faction_role_fa IS NULL OR faction_role_fa = '')"
            ),
            {"role": role, "code": character_code},
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "factions" not in tables:
        op.create_table(
            "factions",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name_fa", sa.String(length=120), nullable=False),
            sa.Column("name_en", sa.String(length=120), nullable=True),
            sa.Column("lore_fa", sa.Text(), nullable=False, server_default=""),
            sa.Column("territory_fa", sa.String(length=160), nullable=True),
            sa.Column("symbol", sa.String(length=16), nullable=True),
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code", name="uq_factions_code"),
        )
        op.create_index("ix_factions_code", "factions", ["code"])

    character_columns = {c["name"] for c in inspector.get_columns("characters")}
    if "faction_id" not in character_columns:
        op.add_column("characters", sa.Column("faction_id", sa.BigInteger(), nullable=True))
        op.create_index("ix_characters_faction_id", "characters", ["faction_id"])
        try:
            op.create_foreign_key(
                "fk_characters_faction",
                "characters",
                "factions",
                ["faction_id"],
                ["id"],
            )
        except NotImplementedError:  # pragma: no cover - e.g. SQLite batch mode
            pass
    if "faction_role_fa" not in character_columns:
        op.add_column(
            "characters", sa.Column("faction_role_fa", sa.String(length=200), nullable=True)
        )

    _insert_missing_factions(bind)
    _backfill_characters(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    character_columns = {c["name"] for c in inspector.get_columns("characters")}

    if "faction_role_fa" in character_columns:
        op.drop_column("characters", "faction_role_fa")
    if "faction_id" in character_columns:
        try:
            op.drop_constraint("fk_characters_faction", "characters", type_="foreignkey")
        except Exception:  # pragma: no cover - dialect without named FK
            pass
        op.drop_column("characters", "faction_id")

    if "factions" in set(inspector.get_table_names()):
        op.drop_table("factions")
