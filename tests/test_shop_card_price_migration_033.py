"""Direct SQLite tests for migration 033 (unified shop card price).

As with the other portable data migrations, the revision runs against a
minimal schema holding the tables it references (``characters``,
``shop_entries``). The test proves that every legacy per-card price and every
historical per-entry override is normalized to the single unified price, and
that the migration also runs (and downgrades) on SQLite, where altering a
column default is not supported and is therefore skipped.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models import SHOP_CARD_PRICE_APPLES

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/033_shop_unified_card_price.py"
UNIFIED_PRICE = 300


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_033", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine: sa.engine.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "characters",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True),
        # Legacy per-card price with the old (migration 012) DB default.
        sa.Column("price", sa.Integer, nullable=False, server_default="100"),
        # The deprecated rarity column is untouched by this migration.
        sa.Column("rarity", sa.String(20), nullable=False, server_default="COMMON"),
    )
    sa.Table(
        "shop_entries",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("rotation_id", sa.BigInteger, nullable=False),
        sa.Column("character_id", sa.BigInteger, nullable=False),
        sa.Column("price_override", sa.Integer, nullable=True),
    )
    metadata.create_all(engine)


def _run(engine: sa.engine.Engine, direction: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def test_revision_chain_points_at_032():
    migration = _load_migration()
    assert migration.revision == "033_shop_card_price"
    assert migration.down_revision == "032_shop_chests"


def test_unified_price_constant_matches_the_models():
    migration = _load_migration()
    assert migration.UNIFIED_CARD_PRICE_APPLES == UNIFIED_PRICE
    assert SHOP_CARD_PRICE_APPLES == UNIFIED_PRICE


def test_033_normalizes_every_legacy_price_and_override(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm033.db'}")
    _legacy_schema(engine)

    with engine.begin() as connection:
        for row_id, price in ((1, 100), (2, 120), (3, 250), (4, 1000), (5, 300)):
            connection.execute(
                sa.text("INSERT INTO characters (id, price, rarity) VALUES (:id, :price, 'LEGENDARY')"),
                {"id": row_id, "price": price},
            )
        connection.execute(
            sa.text(
                "INSERT INTO shop_entries (id, rotation_id, character_id, price_override) "
                "VALUES (1, 1, 1, 5), (2, 1, 2, NULL), (3, 1, 3, 1000)"
            )
        )

    _run(engine, "upgrade")

    with engine.begin() as connection:
        prices = connection.execute(
            sa.text("SELECT id, price FROM characters ORDER BY id")
        ).all()
        overrides = connection.execute(
            sa.text("SELECT id, price_override FROM shop_entries ORDER BY id")
        ).all()
        # The deprecated rarity data is deliberately preserved.
        rarities = {
            row[0] for row in connection.execute(sa.text("SELECT rarity FROM characters")).all()
        }

    assert [price for _id, price in prices] == [UNIFIED_PRICE] * 5
    assert all(override is None for _id, override in overrides)
    assert rarities == {"LEGENDARY"}

    # Downgrade is supported (it reverses the schema default only) and keeps
    # the normalized data - the unified price is a product rule now.
    _run(engine, "downgrade")
    with engine.begin() as connection:
        after = connection.execute(sa.text("SELECT price FROM characters")).all()
    assert [row[0] for row in after] == [UNIFIED_PRICE] * 5


def test_033_upgrade_is_idempotent(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm033_twice.db'}")
    _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO characters (id, price, rarity) VALUES (1, 100, 'COMMON')")
        )

    _run(engine, "upgrade")
    _run(engine, "upgrade")  # re-running changes nothing

    with engine.begin() as connection:
        price = connection.execute(sa.text("SELECT price FROM characters WHERE id = 1")).scalar()
    assert price == UNIFIED_PRICE
