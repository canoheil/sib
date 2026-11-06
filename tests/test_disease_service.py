"""Disease data layer and transactional rules on isolated SQLite databases."""

import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import SystemConfig, User, UserDisease
from sib.services import disease_service as disease
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/070_disease_system.py"
TEHRAN = ZoneInfo("Asia/Tehran")
EXPECTED_CONFIG = {
    "disease_enabled": True,
    "disease_immunity_hours": 24,
    "disease_initial_infection_percent": 15,
    "disease_daily_infection_chance": 10,
    "disease_mine_infection_chance": 2,
    "disease_duel_infection_chance": 5,
    "disease_demon_power_chance": 15,
    "disease_antidote_shop_price": 2000,
    "disease_craft_apple_cost": 200,
    "disease_doctor_daily_limit": 5,
    "disease_doctor_reward_apples": 50,
    "disease_doctor_infection_chance": 10,
}


def _migration():
    spec = importlib.util.spec_from_file_location("migration_070", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine, direction):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(), direction)()


def _config_rows(engine):
    with engine.connect() as connection:
        return dict(connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all())


def _make_migration_baseline(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )


def _foreign_keys_on(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture
def migration_engine():
    engine = sa.create_engine("sqlite:///:memory:")
    sa.event.listen(engine, "connect", _foreign_keys_on)
    _make_migration_baseline(engine)
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sa.event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: User.metadata.create_all(
                conn, tables=[User.__table__, SystemConfig.__table__, UserDisease.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as test_session:
        yield test_session
    await engine.dispose()


@pytest.fixture
def now(monkeypatch):
    moment = datetime(2026, 10, 28, 21, 15, tzinfo=TEHRAN)
    monkeypatch.setattr(disease, "_now", lambda: moment)
    return moment


async def _users(session, *ids):
    session.add_all(User(id=user_id, first_name=f"user {user_id}") for user_id in ids)
    await session.flush()


# Migration contract, including actual Alembic navigation from revision 069.
def test_migration_upgrade_downgrade_is_idempotent_and_preserves_overrides(migration_engine):
    engine = migration_engine
    migration = _migration()
    expected_seed = {key: json.dumps(value) for key, value in EXPECTED_CONFIG.items()}
    assert migration._CONFIG_DEFAULTS == expected_seed
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('disease_enabled', 'false'), "
                "('disease_doctor_daily_limit', '9'), ('unrelated', 'keep')"
            )
        )
    _run_migration(engine, "upgrade")
    inspector = sa.inspect(engine)
    columns = {col["name"]: col for col in inspector.get_columns("user_diseases")}
    assert set(columns) == set(UserDisease.__table__.columns.keys()) == {
        "user_id", "infection_level", "has_demon_power", "infected_at",
        "immunity_until", "last_notified_at",
    }
    assert isinstance(columns["user_id"]["type"], sa.BigInteger)
    assert not columns["user_id"]["nullable"]
    assert not columns["infection_level"]["nullable"]
    assert not columns["has_demon_power"]["nullable"]
    assert all(columns[name]["nullable"] for name in (
        "infected_at", "immunity_until", "last_notified_at"
    ))
    assert all(UserDisease.__table__.c[name].type.timezone for name in (
        "infected_at", "immunity_until", "last_notified_at"
    ))
    assert inspector.get_pk_constraint("user_diseases")["constrained_columns"] == ["user_id"]
    fk = inspector.get_foreign_keys("user_diseases")[0]
    assert (fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]) == (
        ["user_id"], "users", ["id"]
    )
    assert fk["options"]["ondelete"] == "CASCADE"

    expected_seed.update({
        "disease_enabled": "false", "disease_doctor_daily_limit": "9", "unrelated": "keep"
    })
    assert _config_rows(engine) == expected_seed
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (id) VALUES (1)"))
        connection.execute(sa.text("INSERT INTO user_diseases (user_id) VALUES (1)"))
        assert connection.execute(sa.text(
            "SELECT infection_level, has_demon_power, infected_at, immunity_until, "
            "last_notified_at FROM user_diseases WHERE user_id=1"
        )).one() == (0, 0, None, None, None)
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("INSERT INTO user_diseases (user_id) VALUES (999)"))

    _run_migration(engine, "upgrade")
    assert _config_rows(engine) == expected_seed
    with engine.begin() as connection:
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM user_diseases")) == 1
        connection.execute(sa.text("DELETE FROM users WHERE id=1"))
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM user_diseases")) == 0

    _run_migration(engine, "downgrade")
    _run_migration(engine, "downgrade")
    assert not sa.inspect(engine).has_table("user_diseases")
    assert _config_rows(engine) == {
        "disease_enabled": "false", "disease_doctor_daily_limit": "9", "unrelated": "keep"
    }
    _run_migration(engine, "upgrade")
    assert _config_rows(engine) == expected_seed
    assert sa.inspect(engine).has_table("user_diseases")


def test_downgrade_preserves_case_changed_values_with_nocase_collation():
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
            connection.execute(sa.text(
                "CREATE TABLE system_config (`key` VARCHAR(64) COLLATE NOCASE PRIMARY KEY, "
                "`value` TEXT COLLATE NOCASE NOT NULL)"
            ))
            connection.execute(sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('disease_enabled', 'TRUE'), ('DISEASE_IMMUNITY_HOURS', '24')"
            ))
        _run_migration(engine, "upgrade")
        _run_migration(engine, "downgrade")
        assert _config_rows(engine) == {
            "disease_enabled": "TRUE", "DISEASE_IMMUNITY_HOURS": "24"
        }
    finally:
        engine.dispose()


def test_mysql_ddl_and_non_destructive_seeds(monkeypatch):
    migration = _migration()
    # The migration's table-existence check needs a live DB; compile the DDL
    # and seed statements using Alembic's MySQL offline context instead.
    monkeypatch.setattr(migration, "_has_table", lambda: False)
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": output, "literal_binds": True}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()  # _has_table is mocked false; inspect config deletes.
    sql = output.getvalue()
    assert "CREATE TABLE user_diseases" in sql
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in sql
    assert sql.count("ON DUPLICATE KEY UPDATE `key` = `key`") == len(EXPECTED_CONFIG)
    assert sql.count("BINARY `key` = BINARY") == len(EXPECTED_CONFIG)
    assert "DROP TABLE" not in sql


def test_mariadb_seed_uses_duplicate_key_no_op(monkeypatch):
    migration = _migration()
    statements = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: SimpleNamespace(
        dialect=SimpleNamespace(name="mariadb")
    ))
    monkeypatch.setattr(migration.op, "execute", statements.append)
    migration._seed("disease_enabled", "true")
    assert "ON DUPLICATE KEY UPDATE `key` = `key`" in str(statements[0])
    assert statements[0].compile().params == {"key": "disease_enabled", "value": "true"}


def test_alembic_single_head_and_upgrade_from_069(tmp_path):
    migration = _migration()
    assert migration.revision == "070_disease_system"
    assert migration.down_revision == "069_golden_hour_notices"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["080_group_doctors"]

    database = tmp_path / "migration.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    _make_migration_baseline(engine)
    engine.dispose()
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{database}"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT.parent), env.get("PYTHONPATH", "")))

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=ROOT,
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert alembic("heads") == "080_group_doctors (head)"
    alembic("stamp", "069_golden_hour_notices")
    alembic("upgrade", "070_disease_system")
    alembic("upgrade", "070_disease_system")
    assert "070_disease_system" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert sa.inspect(engine).has_table("user_diseases")
    assert _config_rows(engine) == {
        key: json.dumps(value) for key, value in EXPECTED_CONFIG.items()
    }
    engine.dispose()
    alembic("downgrade", "069_golden_hour_notices")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert not sa.inspect(engine).has_table("user_diseases")
    assert _config_rows(engine) == {}
    engine.dispose()
    alembic("upgrade", "070_disease_system")


# Service behavior: no shared DB, Telegram connection, or scheduler is needed.
def test_clock_uses_tehran_timezone():
    assert disease._now().tzinfo == TEHRAN


async def test_missing_config_uses_all_typed_defaults(session):
    assert {key: CONFIG_DEFAULTS[key] for key in EXPECTED_CONFIG} == EXPECTED_CONFIG
    actual = {key: await ConfigService.get(session, key) for key in EXPECTED_CONFIG}
    assert actual == EXPECTED_CONFIG
    assert all(type(actual[key]) is type(value) for key, value in EXPECTED_CONFIG.items())
    assert await session.scalar(sa.select(sa.func.count()).select_from(SystemConfig)) == 0


async def test_infect_healthy_user_once_and_log_source(session, now, monkeypatch, caplog):
    await _users(session, 1)
    roll = Mock(return_value=0.149)
    monkeypatch.setattr(disease.random, "random", roll)
    with caplog.at_level("INFO", logger=disease.__name__):
        assert await disease.infect_user(session, 1, "mine") is True
    row = await session.get(UserDisease, 1)
    assert row.infection_level == 10
    assert disease._as_utc(row.infected_at) == now.astimezone(timezone.utc)
    assert row.has_demon_power is True
    assert await disease.is_sick(session, 1) is True
    assert await disease.is_immune(session, 1) is False
    assert "user_id=1 source=mine" in caplog.text
    assert await disease.infect_user(session, 1, "duel") is False
    assert disease._as_utc(row.infected_at) == now.astimezone(timezone.utc)
    roll.assert_called_once()


@pytest.mark.parametrize(
    "roll,expected", [(0.0, True), (0.1499, True), (0.15, False), (0.99, False)]
)
async def test_default_demon_power_probability(session, now, monkeypatch, roll, expected):
    await _users(session, 1)
    monkeypatch.setattr(disease.random, "random", lambda: roll)
    assert await disease.infect_user(session, 1, "initial")
    assert (await disease.get_status(session, 1))["has_demon_power"] is expected


@pytest.mark.parametrize("flag", [False, "false"])
async def test_disabled_infection_does_not_create_state_but_cure_still_works(
    session, now, monkeypatch, flag
):
    await _users(session, 1)
    await ConfigService.set(session, "disease_enabled", flag)
    roll = Mock()
    monkeypatch.setattr(disease.random, "random", roll)
    assert await disease.infect_user(session, 1, "daily_tick") is False
    assert await session.get(UserDisease, 1) is None
    roll.assert_not_called()
    assert await disease.cure_user(session, 1) is True
    assert await disease.is_immune(session, 1) is True
    assert await disease.is_sick(session, 1) is False


@pytest.mark.parametrize("seconds,immune", [(-1, False), (0, False), (1, True)])
async def test_immunity_boundary_and_reload_with_naive_utc(
    session, now, monkeypatch, seconds, immune
):
    await _users(session, 1)
    deadline = now.astimezone(timezone.utc) + timedelta(seconds=seconds)
    session.add(UserDisease(user_id=1, immunity_until=deadline))
    await session.commit()
    session.expunge_all()  # SQLite reloads DateTime(timezone=True) without tzinfo.
    row = await session.get(UserDisease, 1)
    assert row.immunity_until.tzinfo is None
    assert await disease.is_immune(session, 1) is immune
    monkeypatch.setattr(disease.random, "random", lambda: 0.9)
    assert await disease.infect_user(session, 1, "duel") is (not immune)
    assert await disease.is_sick(session, 1) is (not immune)


async def test_cure_resets_and_sets_configured_immunity_across_reload(session, now, monkeypatch):
    await _users(session, 1)
    monkeypatch.setattr(disease.random, "random", lambda: 0.0)
    assert await disease.infect_user(session, 1, "mine")
    row = await session.get(UserDisease, 1)
    infected_at = row.infected_at
    row.last_notified_at = now.astimezone(timezone.utc) - timedelta(hours=1)
    await ConfigService.set(session, "disease_immunity_hours", 36)
    assert await disease.cure_user(session, 1) is True
    assert (row.infection_level, row.has_demon_power) == (0, False)
    assert row.infected_at == infected_at  # Cure does not erase the prior infection time.
    assert row.last_notified_at is not None
    await session.commit()
    session.expunge_all()
    status = await disease.get_status(session, 1)
    assert status == {
        "is_sick": False, "level": 0, "has_demon_power": False,
        "immunity_until": (now.astimezone(timezone.utc) + timedelta(hours=36)).astimezone(TEHRAN),
    }
    assert status["immunity_until"].tzinfo == TEHRAN
    assert await disease.is_immune(session, 1)
    assert await disease.infect_user(session, 1, "daily_tick") is False


async def test_reinfection_after_cure_expires_rerolls_demon_power(session, now, monkeypatch):
    await _users(session, 1)
    monkeypatch.setattr(disease.random, "random", lambda: 0.5)
    await ConfigService.set(session, "disease_demon_power_chance", 100)
    assert await disease.infect_user(session, 1, "initial")
    assert (await disease.get_status(session, 1))["has_demon_power"] is True
    assert await disease.cure_user(session, 1)
    await session.commit()
    session.expunge_all()
    assert await disease.infect_user(session, 1, "mine") is False

    expiry = now + timedelta(hours=24)
    monkeypatch.setattr(disease, "_now", lambda: expiry)
    await ConfigService.set(session, "disease_demon_power_chance", 0)
    assert await disease.is_immune(session, 1) is False
    assert await disease.infect_user(session, 1, "mine") is True
    row = await session.get(UserDisease, 1)
    assert disease._as_utc(row.infected_at) == expiry.astimezone(timezone.utc)
    assert row.has_demon_power is False
    assert disease._as_utc(row.immunity_until) == expiry.astimezone(timezone.utc)


async def test_default_immunity_is_24_elapsed_hours_across_tehran_dst(session, monkeypatch):
    await _users(session, 1)
    before_dst = datetime(2021, 3, 21, 22, 30, tzinfo=TEHRAN)
    monkeypatch.setattr(disease, "_now", lambda: before_dst)
    assert await disease.cure_user(session, 1)
    expected = (before_dst.astimezone(timezone.utc) + timedelta(hours=24)).astimezone(TEHRAN)
    assert (await disease.get_status(session, 1))["immunity_until"] == expected
    assert expected.hour == 23  # The local clock jumped forward during immunity.
    monkeypatch.setattr(disease, "_now", lambda: expected)
    assert await disease.is_immune(session, 1) is False


async def test_status_for_missing_and_existing_rows(session, now):
    await _users(session, 1, 2)
    assert await disease.get_status(session, 1) == {
        "is_sick": False, "level": 0, "has_demon_power": False, "immunity_until": None
    }
    assert await disease.is_sick(session, 1) is False
    assert await disease.is_immune(session, 1) is False
    assert await session.get(UserDisease, 1) is None
    expired = now.astimezone(timezone.utc) - timedelta(hours=2)
    session.add(UserDisease(
        user_id=2, infection_level=77, has_demon_power=True, immunity_until=expired
    ))
    await session.commit()
    session.expunge_all()
    assert await disease.get_status(session, 2) == {
        "is_sick": True, "level": 77, "has_demon_power": True,
        "immunity_until": expired.astimezone(TEHRAN),
    }
    assert await disease.is_immune(session, 2) is False


async def test_initial_sampling_uses_default_percentage_and_distinct_players(
    session, now, monkeypatch
):
    await _users(session, *range(1, 21))
    sample = Mock(return_value=[17, 8, 3])
    monkeypatch.setattr(disease.random, "sample", sample)
    monkeypatch.setattr(disease.random, "random", lambda: 0.9)
    assert await disease.process_initial_infection(session, list(range(1, 21)) + [1, 2]) == 3
    sample.assert_called_once_with(list(range(1, 21)), 3)
    infected_ids = (await session.scalars(sa.select(UserDisease.user_id))).all()
    assert set(infected_ids) == {3, 8, 17}


async def test_initial_sampling_rounds_down_and_counts_only_new_infections(
    session, now, monkeypatch
):
    await _users(session, *range(1, 8))
    sample = Mock(return_value=[4])
    monkeypatch.setattr(disease.random, "sample", sample)
    monkeypatch.setattr(disease.random, "random", lambda: 0.9)
    assert await disease.process_initial_infection(session, list(range(1, 7))) == 0
    sample.assert_not_called()
    session.add(UserDisease(user_id=4, infection_level=10))
    await session.flush()
    assert await disease.process_initial_infection(session, list(range(1, 8))) == 0
    sample.assert_called_once_with(list(range(1, 8)), 1)


async def test_daily_tick_uses_default_chance_and_counts_only_new_infections(
    session, now, monkeypatch
):
    await _users(session, *range(1, 6))
    session.add_all([
        UserDisease(user_id=3, infection_level=10),
        UserDisease(user_id=4, immunity_until=now.astimezone(timezone.utc) + timedelta(hours=1)),
    ])
    await session.flush()
    # Sorted users 1..5: hit + demon miss, exact 10% miss, sick hit,
    # immune hit, hit + demon miss.
    rolls = Mock(side_effect=[0.05, 0.8, 0.10, 0.09, 0.08, 0.01, 0.99])
    monkeypatch.setattr(disease.random, "random", rolls)
    assert await disease.process_daily_infection_tick(session, [5, 3, 1, 4, 2]) == 2
    assert rolls.call_count == 7
    assert [await disease.is_sick(session, i) for i in range(1, 6)] == [
        True, False, True, False, True
    ]
    assert await disease.is_immune(session, 4)


async def test_configured_percentages_are_clamped_and_honor_zero(session, now, monkeypatch):
    await _users(session, 1, 2)
    monkeypatch.setattr(disease.random, "random", lambda: 0.999999)
    await ConfigService.set(session, "disease_initial_infection_percent", -1)
    assert await disease.process_initial_infection(session, [1, 2]) == 0
    await ConfigService.set(session, "disease_daily_infection_chance", 1000)
    await ConfigService.set(session, "disease_demon_power_chance", 100)
    assert await disease.process_daily_infection_tick(session, [2, 1]) == 2
    assert (await disease.get_status(session, 1))["has_demon_power"] is True
    await ConfigService.set(session, "disease_immunity_hours", 0)
    assert await disease.cure_user(session, 1)
    assert await disease.is_immune(session, 1) is False
    monkeypatch.setattr(disease.random, "random", lambda: 0.0)
    await ConfigService.set(session, "disease_daily_infection_chance", 0)
    assert await disease.process_daily_infection_tick(session, [1]) == 0
    await ConfigService.set(session, "disease_demon_power_chance", 0)
    assert await disease.infect_user(session, 1, "initial")
    assert (await disease.get_status(session, 1))["has_demon_power"] is False


async def test_disabled_batches_cannot_infect_anyone(session, now, monkeypatch):
    await _users(session, 1, 2)
    await ConfigService.set(session, "disease_enabled", False)
    await ConfigService.set(session, "disease_initial_infection_percent", 100)
    await ConfigService.set(session, "disease_daily_infection_chance", 100)
    monkeypatch.setattr(disease.random, "random", lambda: 0.0)
    assert await disease.process_initial_infection(session, [1, 2]) == 0
    assert await disease.process_daily_infection_tick(session, [1, 2]) == 0
    assert await session.scalar(sa.select(sa.func.count()).select_from(UserDisease)) == 0


async def test_invalid_hours_fall_back_and_negative_hours_are_applied(session, now):
    await _users(session, 1)
    await ConfigService.set(session, "disease_immunity_hours", "invalid")
    assert await disease.cure_user(session, 1)
    assert (await disease.get_status(session, 1))["immunity_until"] == now + timedelta(hours=24)
    await ConfigService.set(session, "disease_immunity_hours", -2)
    assert await disease.cure_user(session, 1)
    assert (await disease.get_status(session, 1))["immunity_until"] == now - timedelta(hours=2)
    assert await disease.is_immune(session, 1) is False


async def test_mutations_require_an_existing_user(session, now):
    assert await disease.get_status(session, 404) == {
        "is_sick": False, "level": 0, "has_demon_power": False, "immunity_until": None
    }
    with pytest.raises(ValueError, match="unknown user 404"):
        await disease.infect_user(session, 404, "initial")
    with pytest.raises(ValueError, match="unknown user 404"):
        await disease.cure_user(session, 404)
    assert await session.get(UserDisease, 404) is None


async def test_mutations_are_owned_by_the_callers_transaction(session, now, monkeypatch):
    await _users(session, 1)
    await session.commit()
    monkeypatch.setattr(disease.random, "random", lambda: 0.9)
    assert await disease.infect_user(session, 1, "initial")
    await session.rollback()
    assert await session.get(UserDisease, 1) is None
