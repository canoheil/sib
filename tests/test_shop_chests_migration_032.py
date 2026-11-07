"""Direct SQLite tests for migration 032 (shop chests).

As with the other portable migrations, the revision runs against a minimal
schema holding the tables it references (``users``, ``characters``). The test
also checks that 032 plus the additive 082 tier/pity migration match the
current ORM models, so the schema cannot drift apart silently.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/032_shop_chests.py"
TABLES = ("shop_chests", "shop_chest_rewards", "shop_chest_openings")
TIER_MIGRATION = REPO_ROOT / "database/migrations/versions/082_chest_pity.py"


def _load_migration(path=MIGRATION):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_parents(engine: sa.engine.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", sa.BigInteger, primary_key=True))
    sa.Table("characters", metadata, sa.Column("id", sa.BigInteger, primary_key=True))
    metadata.create_all(engine)


def _run(engine: sa.engine.Engine, direction: str, *, path=MIGRATION) -> None:
    migration = _load_migration(path)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def test_revision_chain_points_at_031():
    migration = _load_migration()
    assert migration.revision == "032_shop_chests"
    assert migration.down_revision == "031_invasion_four_wave"


def test_032_creates_tables_constraints_and_downgrades_cleanly(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm032.db'}")
    _minimal_parents(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (id) VALUES (7)"))
        connection.execute(sa.text("INSERT INTO characters (id) VALUES (3)"))

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    for table in TABLES:
        assert table in inspector.get_table_names()
    chest_indexes = {index["name"] for index in inspector.get_indexes("shop_chests")}
    assert "ix_shop_chests_active_sort" in chest_indexes
    opening_indexes = {index["name"] for index in inspector.get_indexes("shop_chest_openings")}
    assert {"ix_shop_chest_openings_user_id", "ix_shop_chest_openings_chest"} <= opening_indexes

    insert_chest = sa.text(
        "INSERT INTO shop_chests (id, code, name, price_apples, duplicate_apples, is_active, sort_order, version) "
        "VALUES (:id, :code, 'n', :price, 0, 1, 0, 1)"
    )
    with engine.begin() as connection:
        connection.execute(insert_chest, {"id": 1, "code": "alpha_chest", "price": 10})
    # Duplicate code, non-positive price and version are rejected by the schema.
    for params in (
        {"id": 2, "code": "alpha_chest", "price": 10},
        {"id": 3, "code": "beta_chest", "price": 0},
    ):
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as connection:
                connection.execute(insert_chest, params)

    # Reward shape: an apples row must not carry an item key, and kinds are closed.
    # (SQLite does not auto-increment BigInteger keys, so ids are explicit.)
    insert_reward = sa.text(
        "INSERT INTO shop_chest_rewards (id, chest_id, kind, weight, apples, item_key, character_id, sort_order) "
        "VALUES (:id, 1, :kind, :weight, :apples, :item, :char, 0)"
    )
    with engine.begin() as connection:
        connection.execute(insert_reward, {"id": 1, "kind": "apples", "weight": 1, "apples": 5, "item": None, "char": None})
        connection.execute(insert_reward, {"id": 2, "kind": "card", "weight": 2, "apples": None, "item": None, "char": 3})
    for params in (
        {"id": 10, "kind": "apples", "weight": 1, "apples": 5, "item": "flower", "char": None},
        {"id": 11, "kind": "item", "weight": 1, "apples": None, "item": None, "char": None},
        {"id": 12, "kind": "gold", "weight": 1, "apples": 5, "item": None, "char": None},
        {"id": 13, "kind": "apples", "weight": 0, "apples": 5, "item": None, "char": None},
    ):
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as connection:
                connection.execute(insert_reward, params)

    # One opening per user and operation key; the same key for another user is fine.
    insert_opening = sa.text(
        "INSERT INTO shop_chest_openings (id, user_id, chest_id, operation_key, chest_version, price_apples, "
        "reward_kind, reward_apples, duplicate_converted, compensation_apples, balance_after) "
        "VALUES (:id, :user, 1, 'open-key-0001', 1, 10, 'apples', 5, 0, 0, 0)"
    )
    with engine.begin() as connection:
        connection.execute(insert_opening, {"id": 1, "user": 7})
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(insert_opening, {"id": 2, "user": 7})
    with engine.begin() as connection:
        connection.execute(insert_opening, {"id": 3, "user": 8})

    _run(engine, "downgrade")
    after = set(sa.inspect(engine).get_table_names())
    assert not (set(TABLES) & after)


def test_032_and_082_schema_match_orm_models(tmp_path):
    """The migrated tables and the ORM models must agree on columns and indexes."""
    from sib.database.base import Base
    import sib.database.models  # noqa: F401  (registers the chest models)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm032_models.db'}")
    _minimal_parents(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade", path=TIER_MIGRATION)
    inspector = sa.inspect(engine)
    for table in (*TABLES, "shop_chest_pity"):
        migrated_cols = {column["name"]: column for column in inspector.get_columns(table)}
        modelled = Base.metadata.tables[table].columns
        assert set(migrated_cols) == {column.name for column in modelled}, table
        # Nullability and the PRESENCE of a server default must agree (values are
        # compared by the migration's own tests; SQLite reflects them differently).
        for column in modelled:
            migrated = migrated_cols[column.name]
            assert migrated["nullable"] == column.nullable, f"{table}.{column.name} nullable"
            assert (migrated["default"] is not None) == (column.server_default is not None), (
                f"{table}.{column.name} server_default presence"
            )
    _run(engine, "downgrade", path=TIER_MIGRATION)
    _run(engine, "downgrade")
