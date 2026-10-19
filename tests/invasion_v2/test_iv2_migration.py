"""Frozen IV2 data migration: old defaults, price-map merge and safe retries."""
import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select

from sib.database.models import Base, SystemConfig
from sib.tests.test_migrations import _run_alembic


ROOT = Path(__file__).resolve().parents[2]


def _migration():
    path = ROOT / "database/migrations/versions/047_inv2_rebalance_sib_buff.py"
    spec = importlib.util.spec_from_file_location("migration_iv2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _values(connection):
    return dict(connection.execute(select(SystemConfig.key, SystemConfig.value)).all())


@pytest.fixture
def config_db():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection, tables=[SystemConfig.__table__])
            connection.execute(SystemConfig.__table__.insert(), [
                {"key": "invasion_enabled", "value": "false"},
                {"key": "unrelated", "value": "7200"},
            ])
            yield connection
    finally:
        engine.dispose()


@pytest.mark.parametrize("old_interval", ["3600", "7200"])
def test_iv2_migration_updates_known_defaults_and_merges_shop(config_db, old_interval):
    connection = config_db
    prices = {"magic_powder": 41, "wedding_ring": 1200, "custom_item": 9}
    connection.execute(SystemConfig.__table__.insert(), [
        {"key": "invasion_difficulty_multiplier", "value": "2.5"},
        {"key": "invasion_join_window_seconds", "value": "300"},
        {"key": "invasion_interval_seconds", "value": old_interval},
        {"key": "item_buy_prices", "value": json.dumps(prices)},
    ])
    with Operations.context(MigrationContext.configure(connection)):
        _migration().upgrade()
        _migration().upgrade()
    values = _values(connection)
    assert values["invasion_difficulty_multiplier"] == "2.0"
    assert values["invasion_join_window_seconds"] == "1800"
    assert values["invasion_interval_seconds"] == "14400"
    assert json.loads(values["item_buy_prices"]) == {**prices, "sib_buff": 50}
    assert values["invasion_enabled"] == "false" and values["unrelated"] == "7200"
    with Operations.context(MigrationContext.configure(connection)):
        _migration().downgrade()
        _migration().downgrade()
    values = _values(connection)
    assert (values["invasion_difficulty_multiplier"], values["invasion_join_window_seconds"],
            values["invasion_interval_seconds"]) == ("2.5", "300", "3600")
    assert json.loads(values["item_buy_prices"]) == prices
    assert values["invasion_enabled"] == "false"


def test_iv2_migration_preserves_custom_admin_values_and_existing_buff_price(config_db):
    connection = config_db
    custom = {
        "invasion_difficulty_multiplier": "1.25",
        "invasion_join_window_seconds": "900",
        "invasion_interval_seconds": "20000",
        "item_buy_prices": json.dumps({"magic_powder": 10, "sib_buff": 75}),
    }
    connection.execute(SystemConfig.__table__.insert(), [
        {"key": key, "value": value} for key, value in custom.items()])
    before = _values(connection)
    with Operations.context(MigrationContext.configure(connection)):
        _migration().upgrade()
        _migration().downgrade()
    assert _values(connection) == before


def test_iv2_migration_missing_rows_use_code_defaults_without_seeding(config_db):
    before = _values(config_db)
    with Operations.context(MigrationContext.configure(config_db)):
        _migration().upgrade()
        _migration().downgrade()
    assert _values(config_db) == before


@pytest.mark.parametrize("raw", ["[]", "null", '"not a price map"', "not-json"])
def test_iv2_migration_refuses_to_replace_invalid_price_map(config_db, raw):
    config_db.execute(SystemConfig.__table__.insert(), {"key": "item_buy_prices", "value": raw})
    with Operations.context(MigrationContext.configure(config_db)):
        with pytest.raises(ValueError):
            _migration().upgrade()
    assert _values(config_db)["item_buy_prices"] == raw


def test_iv2_migration_downgrade_preserves_subsequent_admin_edits(config_db):
    config_db.execute(SystemConfig.__table__.insert(), [
        {"key": "invasion_difficulty_multiplier", "value": "3.2"},
        {"key": "invasion_join_window_seconds", "value": "1200"},
        {"key": "invasion_interval_seconds", "value": "28800"},
        {"key": "item_buy_prices", "value": '{"sib_buff": 55}'},
    ])
    before = _values(config_db)
    with Operations.context(MigrationContext.configure(config_db)):
        _migration().downgrade()
    assert _values(config_db) == before


def test_iv2_alembic_upgrade_and_downgrade_from_046(tmp_path):
    url = f"sqlite:///{tmp_path / 'iv2_upgrade.db'}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection, tables=[SystemConfig.__table__])
            connection.execute(SystemConfig.__table__.insert(), [
                {"key": "invasion_difficulty_multiplier", "value": "2.5"},
                {"key": "invasion_join_window_seconds", "value": "300"},
                {"key": "invasion_interval_seconds", "value": "7200"},
                {"key": "item_buy_prices", "value": '{"magic_powder": 40}'},
                {"key": "invasion_enabled", "value": "false"},
            ])
        result = _run_alembic("stamp", "046_inv2_min_players_with_card", db_url=url)
        assert result.returncode == 0, result.stderr
        # Pinned to the last revision this synthetic config-only database can
        # execute (051+ alters core tables such as ``users``); the assertions
        # below only exercise the 047 data migration anyway.
        result = _run_alembic("upgrade", "050_apple_clash", db_url=url)
        assert result.returncode == 0, result.stderr
        with engine.connect() as connection:
            values = _values(connection)
            assert values["invasion_interval_seconds"] == "14400"
            assert values["invasion_join_window_seconds"] == "1800"
            assert values["invasion_difficulty_multiplier"] == "2.0"
            assert json.loads(values["item_buy_prices"])["sib_buff"] == 50
            assert values["invasion_enabled"] == "false"
        result = _run_alembic("downgrade", "046_inv2_min_players_with_card", db_url=url)
        assert result.returncode == 0, result.stderr
        with engine.connect() as connection:
            assert _values(connection)["invasion_interval_seconds"] == "3600"
        result = _run_alembic("upgrade", "050_apple_clash", db_url=url)
        assert result.returncode == 0, result.stderr
    finally:
        engine.dispose()
