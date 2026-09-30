"""Card Upgrade System: derive card stats from level, retire manual allocation.

Revision ID: 022_card_upgrade_levels
Revises: 021_bot_fallback_matchmaking
Create Date: 2026-09-30

Audit of the old system (migration 013 + PlayerCardService.upgrade/reallocate):
it DID persist modified attribute values - ``player_cards.power/speed/
intelligence`` held the player's chosen allocation and ``boosted_*`` counted
the points added through upgrades, next to ``level`` (1..n). The new model
keeps only ``level`` on the owned card; attributes are derived as
``characters.<attr> + 10 * (level - 1)``.

Safe data handling:
1. every existing card row is COPIED (level, stored and boosted attributes)
   into ``player_card_legacy_stats`` first - nothing a player had is lost;
2. every card (and the Apple Battle loadout level mirroring it) becomes
   Level 1; ownership, card identity, favorite flag and timestamps are
   untouched;
3. only then the stored attribute/boost columns are dropped.

Downgrade restores the archived values (cards created after this migration
fall back to their character's base attributes) and drops the archive.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "022_card_upgrade_levels"
down_revision: Union[str, None] = "021_bot_fallback_matchmaking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_COLUMNS = (
    "power",
    "speed",
    "intelligence",
    "boosted_power",
    "boosted_speed",
    "boosted_intelligence",
)


def _player_cards() -> sa.TableClause:
    return sa.table(
        "player_cards",
        sa.column("id", sa.BigInteger()),
        sa.column("user_id", sa.BigInteger()),
        sa.column("character_id", sa.BigInteger()),
        sa.column("level", sa.Integer()),
        *(sa.column(name, sa.Integer()) for name in _LEGACY_COLUMNS),
    )


def _legacy() -> sa.TableClause:
    return sa.table(
        "player_card_legacy_stats",
        sa.column("card_id", sa.BigInteger()),
        sa.column("user_id", sa.BigInteger()),
        sa.column("character_id", sa.BigInteger()),
        sa.column("level", sa.Integer()),
        *(sa.column(name, sa.Integer()) for name in _LEGACY_COLUMNS),
    )


def archive_and_reset_levels(bind) -> None:
    """Copy every card's legacy data to the archive, then reset to Level 1."""
    cards = _player_cards()
    legacy = _legacy()
    columns = ["card_id", "user_id", "character_id", "level", *_LEGACY_COLUMNS]
    bind.execute(
        sa.insert(legacy).from_select(
            columns,
            sa.select(
                cards.c.id,
                cards.c.user_id,
                cards.c.character_id,
                cards.c.level,
                *(cards.c[name] for name in _LEGACY_COLUMNS),
            ),
        )
    )
    bind.execute(sa.update(cards).values(level=1))
    # The Apple Battle loadout mirrors its card's level.
    if sa.inspect(bind).has_table("apple_battle_queue"):
        queue = sa.table("apple_battle_queue", sa.column("level", sa.Integer()))
        bind.execute(sa.update(queue).values(level=1))


def upgrade() -> None:
    op.create_table(
        "player_card_legacy_stats",
        sa.Column("card_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("character_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        *(sa.Column(name, sa.Integer(), nullable=False) for name in _LEGACY_COLUMNS),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("card_id"),
    )
    archive_and_reset_levels(op.get_bind())
    with op.batch_alter_table("player_cards") as batch:
        for name in _LEGACY_COLUMNS:
            batch.drop_column(name)


def restore_legacy_stats(bind) -> None:
    """Refill stored attributes: archive first, character base as fallback."""
    cards = _player_cards()
    legacy = _legacy()
    characters = sa.table(
        "characters",
        sa.column("id", sa.BigInteger()),
        sa.column("power", sa.Integer()),
        sa.column("speed", sa.Integer()),
        sa.column("intelligence", sa.Integer()),
    )
    values = {}
    for name in _LEGACY_COLUMNS:
        archived = (
            sa.select(legacy.c[name]).where(legacy.c.card_id == cards.c.id).scalar_subquery()
        )
        if name.startswith("boosted_"):
            fallback = sa.literal(0)
        else:
            fallback = (
                sa.select(characters.c[name])
                .where(characters.c.id == cards.c.character_id)
                .scalar_subquery()
            )
        values[name] = sa.func.coalesce(archived, fallback, 20 if not name.startswith("boosted_") else 0)
    archived_level = (
        sa.select(legacy.c.level).where(legacy.c.card_id == cards.c.id).scalar_subquery()
    )
    values["level"] = sa.func.coalesce(archived_level, cards.c.level)
    bind.execute(sa.update(cards).values(**values))


def downgrade() -> None:
    defaults = {
        "power": "20",
        "speed": "20",
        "intelligence": "20",
        "boosted_power": "0",
        "boosted_speed": "0",
        "boosted_intelligence": "0",
    }
    with op.batch_alter_table("player_cards") as batch:
        for name in _LEGACY_COLUMNS:
            batch.add_column(
                sa.Column(name, sa.Integer(), nullable=False, server_default=defaults[name])
            )
    restore_legacy_stats(op.get_bind())
    op.drop_table("player_card_legacy_stats")
