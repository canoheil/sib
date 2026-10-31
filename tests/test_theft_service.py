"""Apple theft and wanted-level rules on isolated SQLite databases.

Backend only: this module exercises the data layer, the transactional service
and migration 074. No Telegram handlers exist for theft yet.
"""

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    SystemConfig,
    User,
    UserWantedState,
)
from sib.services import theft_service as theft
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import CONFIG_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/074_theft_system.py"
TEHRAN = ZoneInfo("Asia/Tehran")
EXPECTED_CONFIG = {
    "theft_cooldown_minutes": 20,
    "theft_jail_hours": 2,
    "theft_amount_percent": 5,
    "theft_min_amount": 50,
    "theft_max_amount": 500,
    "theft_arrest_window_seconds": 10,
    "theft_arrest_reward_per_star": 100,
}
EXPECTED_SEED = {key: str(value) for key, value in EXPECTED_CONFIG.items()}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _migration():
    spec = importlib.util.spec_from_file_location("migration_074", MIGRATION)
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
        return dict(
            connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
        )


def _foreign_keys_on(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _make_migration_baseline(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )


@pytest.fixture
def migration_engine():
    engine = sa.create_engine("sqlite:///:memory:")
    sa.event.listen(engine, "connect", _foreign_keys_on)
    _make_migration_baseline(engine)
    yield engine
    engine.dispose()


@contextmanager
def _sqlite_integer_pks():
    """Narrow autoincrement BIGINT PKs to INTEGER for SQLite DDL only.

    Mirrors ``tests/conftest.py``; the original column types are restored so
    the shared model metadata is never left modified.
    """
    changed = []
    for table in (AppleWallet.__table__, AppleTransaction.__table__):
        for column in table.columns:
            if column.primary_key and type(column.type).__name__ == "BigInteger":
                changed.append((column, column.type))
                column.type = sa.Integer()
    try:
        yield
    finally:
        for column, original in changed:
            column.type = original


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sa.event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    tables = [
        User.__table__,
        SystemConfig.__table__,
        AppleWallet.__table__,
        AppleTransaction.__table__,
        UserWantedState.__table__,
    ]
    with _sqlite_integer_pks():
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda conn: User.metadata.create_all(conn, tables=tables)
            )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as test_session:
        yield test_session
    await engine.dispose()


@pytest.fixture
def now(monkeypatch):
    moment = datetime(2026, 10, 30, 21, 15, tzinfo=TEHRAN)
    monkeypatch.setattr(theft, "_now", lambda: moment)
    return moment


def _utc(moment):
    return moment.astimezone(timezone.utc)


def _aware(value):
    """SQLite returns naive DATETIMEs even for timezone-aware columns."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


async def _users(session, *ids):
    session.add_all(User(id=user_id, first_name=f"user {user_id}") for user_id in ids)
    await session.flush()


async def _wallet(session, user_id, balance):
    wallet = AppleWallet(user_id=user_id, balance=balance)
    session.add(wallet)
    await session.flush()
    return wallet


async def _state(session, user_id, **values):
    state = UserWantedState(user_id=user_id, **values)
    session.add(state)
    await session.flush()
    return state


async def _set_config(session, key, value):
    session.add(SystemConfig(key=key, value=json.dumps(value)))
    await session.flush()


async def _balance(session, user_id):
    return await AppleWalletService.get_balance(session, user_id)


async def _transactions(session):
    rows = await session.scalars(
        sa.select(AppleTransaction).order_by(AppleTransaction.id)
    )
    return list(rows.all())


# ---------------------------------------------------------------------------
# Migration 074
# ---------------------------------------------------------------------------

def test_migration_074_revises_073_and_075_is_the_single_head():
    migration = _migration()
    assert migration.revision == "074_theft_system"
    assert migration.down_revision == "073_disease_started_flag"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["075_rocket_game"]
    assert scripts.get_revision("074_theft_system").down_revision == "073_disease_started_flag"


def test_config_defaults_match_the_migration_seed():
    assert {key: CONFIG_DEFAULTS[key] for key in EXPECTED_CONFIG} == EXPECTED_CONFIG
    assert _migration()._CONFIG_DEFAULTS == EXPECTED_SEED


def test_migration_upgrade_creates_table_seeds_and_preserves_overrides(migration_engine):
    engine = migration_engine
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('theft_jail_hours', '5'), ('unrelated', 'keep')"
            )
        )

    _run_migration(engine, "upgrade")
    _run_migration(engine, "upgrade")  # idempotent

    inspector = sa.inspect(engine)
    columns = {col["name"]: col for col in inspector.get_columns("user_wanted_states")}
    assert set(columns) == set(UserWantedState.__table__.columns.keys()) == {
        "user_id", "wanted_level", "last_stolen_at", "wanted_expires_at", "jailed_until",
    }
    assert isinstance(columns["user_id"]["type"], sa.BigInteger)
    assert not columns["user_id"]["nullable"]
    assert not columns["wanted_level"]["nullable"]
    assert all(
        columns[name]["nullable"]
        for name in ("last_stolen_at", "wanted_expires_at", "jailed_until")
    )
    assert all(
        UserWantedState.__table__.c[name].type.timezone
        for name in ("last_stolen_at", "wanted_expires_at", "jailed_until")
    )
    assert inspector.get_pk_constraint("user_wanted_states")["constrained_columns"] == [
        "user_id"
    ]
    fk = inspector.get_foreign_keys("user_wanted_states")[0]
    assert (
        fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]
    ) == (["user_id"], "users", ["id"])
    assert fk["options"]["ondelete"] == "CASCADE"

    expected_seed = dict(EXPECTED_SEED)
    expected_seed.update({"theft_jail_hours": "5", "unrelated": "keep"})
    assert _config_rows(engine) == expected_seed

    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (id) VALUES (1)"))
        connection.execute(sa.text("INSERT INTO user_wanted_states (user_id) VALUES (1)"))
        assert connection.execute(
            sa.text(
                "SELECT wanted_level, last_stolen_at, wanted_expires_at, jailed_until "
                "FROM user_wanted_states WHERE user_id=1"
            )
        ).one() == (0, None, None, None)
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("INSERT INTO user_wanted_states (user_id) VALUES (999)"))

    _run_migration(engine, "downgrade")
    _run_migration(engine, "downgrade")  # idempotent
    assert not sa.inspect(engine).has_table("user_wanted_states")
    # An operator override survives; untouched defaults are removed.
    assert _config_rows(engine) == {"theft_jail_hours": "5", "unrelated": "keep"}


def test_alembic_real_upgrade_and_downgrade_from_073(tmp_path):
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["075_rocket_game"]

    database = tmp_path / "theft_migration.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    _make_migration_baseline(engine)
    engine.dispose()
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{database}"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT.parent), env.get("PYTHONPATH", "")))

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert alembic("heads") == "075_rocket_game (head)"
    alembic("stamp", "073_disease_started_flag")
    alembic("upgrade", "074_theft_system")
    alembic("upgrade", "074_theft_system")
    assert "074_theft_system" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert sa.inspect(engine).has_table("user_wanted_states")
    assert _config_rows(engine) == EXPECTED_SEED
    engine.dispose()

    alembic("downgrade", "073_disease_started_flag")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert not sa.inspect(engine).has_table("user_wanted_states")
    assert _config_rows(engine) == {}
    engine.dispose()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

async def test_status_without_a_row_is_clean(session, now):
    await _users(session, 1)
    assert await theft.get_status(session, 1) == {
        "wanted_level": 0,
        "is_jailed": False,
        "jail_remaining_seconds": 0,
        "is_cooldown": False,
    }


async def test_status_clears_an_expired_wanted_level(session, now):
    await _users(session, 1)
    await _state(
        session,
        1,
        wanted_level=3,
        wanted_expires_at=_utc(now) - timedelta(seconds=1),
        last_stolen_at=_utc(now) - timedelta(hours=48),
    )
    status = await theft.get_status(session, 1)
    assert status["wanted_level"] == 0
    assert status["is_cooldown"] is False
    # The reset is persisted, not just masked.
    assert await session.scalar(
        sa.select(UserWantedState.wanted_level).where(UserWantedState.user_id == 1)
    ) == 0
    assert await session.scalar(
        sa.select(UserWantedState.wanted_expires_at).where(UserWantedState.user_id == 1)
    ) is None


async def test_status_keeps_a_live_wanted_level(session, now):
    await _users(session, 1)
    await _state(session, 1, wanted_level=4, wanted_expires_at=_utc(now) + timedelta(hours=1))
    assert (await theft.get_status(session, 1))["wanted_level"] == 4


async def test_status_reports_jail_and_cooldown_remaining(session, now):
    await _users(session, 1)
    await _state(
        session,
        1,
        wanted_level=2,
        wanted_expires_at=_utc(now) + timedelta(hours=5),
        jailed_until=_utc(now) + timedelta(minutes=30),
        last_stolen_at=_utc(now) - timedelta(minutes=5),
    )
    assert await theft.get_status(session, 1) == {
        "wanted_level": 2,
        "is_jailed": True,
        "jail_remaining_seconds": 1800,
        "is_cooldown": True,
    }


async def test_status_after_jail_and_cooldown_expire(session, now):
    await _users(session, 1)
    await _state(
        session,
        1,
        jailed_until=_utc(now) - timedelta(seconds=1),
        last_stolen_at=_utc(now) - timedelta(minutes=20),
    )
    status = await theft.get_status(session, 1)
    assert status["is_jailed"] is False
    assert status["jail_remaining_seconds"] == 0
    assert status["is_cooldown"] is False


# ---------------------------------------------------------------------------
# initiate_theft: refusals
# ---------------------------------------------------------------------------

async def test_jailed_thief_cannot_steal(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(session, 2, wanted_level=2, jailed_until=_utc(now) + timedelta(hours=1))

    assert await theft.initiate_theft(session, 2, 1) == {"status": "jailed"}
    assert await _balance(session, 1) == 4000
    assert await _balance(session, 2) == 0
    assert await _transactions(session) == []


async def test_cooldown_blocks_a_second_theft(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session,
        2,
        wanted_level=1,
        wanted_expires_at=_utc(now) + timedelta(hours=5),
        last_stolen_at=_utc(now) - timedelta(minutes=5),
    )

    assert await theft.initiate_theft(session, 2, 1) == {"status": "cooldown"}
    assert await _balance(session, 1) == 4000
    assert await _transactions(session) == []


async def test_cooldown_expires_after_the_configured_minutes(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session,
        2,
        wanted_level=1,
        wanted_expires_at=_utc(now) + timedelta(hours=5),
        last_stolen_at=_utc(now) - timedelta(minutes=20),
    )

    result = await theft.initiate_theft(session, 2, 1)
    # One star: the cooldown is over, but the heist now waits for the window.
    assert result == {"status": "pending", "amount": 200, "window_seconds": 10}
    assert await _balance(session, 1) == 4000


async def test_poor_victim_cannot_be_robbed(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 49)
    await _wallet(session, 2, 0)

    assert await theft.initiate_theft(session, 2, 1) == {"status": "insufficient"}
    assert await _balance(session, 1) == 49
    assert await _transactions(session) == []


async def test_theft_refuses_self_targets_and_unknown_users(session, now):
    await _users(session, 1)
    await _wallet(session, 1, 4000)
    assert await theft.initiate_theft(session, 1, 1) == {"status": "invalid"}
    assert await theft.initiate_theft(session, 1, 999) == {"status": "not_found"}


# ---------------------------------------------------------------------------
# The exact behaviour the theft fix locks in (bugs 1 and 2):
#   level 0 -> instant, 100 %      level 1 -> pending, 90 %
#   level 2 -> pending, 80 %       level 3 -> pending, 70 %
#   level 4 -> pending, 60 %       level 5 -> pending, 50 %
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,expected_chance", [(0, 100), (1, 90), (2, 80), (3, 70), (4, 60), (5, 50)]
)
def test_success_percent_drops_ten_points_per_star(level, expected_chance):
    assert theft.success_percent(level) == expected_chance
    # Wanted thieves never fall below the 50 % floor above the five stars.
    assert theft.success_percent(level + 7) == 50
    assert theft.success_percent(99) == 50


async def test_wanted_level_zero_is_an_instant_guaranteed_theft(session, now):
    """Test 1: a clean thief always succeeds and no window opens."""
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(session, 2, wanted_level=0)

    result = await theft.initiate_theft(session, 2, 1)

    assert result == {"status": "success", "amount": 200, "wanted_level": 1}
    assert "window_seconds" not in result
    assert await _balance(session, 2) == 200


async def test_wanted_level_one_opens_the_window_and_rolls_ninety(
    session, now, monkeypatch
):
    """Test 2: one star is a pending heist that rolls the 90 % row."""
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=1, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    started = await theft.initiate_theft(session, 2, 1)

    assert started == {"status": "pending", "amount": 200, "window_seconds": 10}
    assert await _balance(session, 1) == 4000  # nothing moves before the window

    rolled: list[int] = []

    def spy(percent: int) -> bool:
        rolled.append(percent)
        return True

    monkeypatch.setattr(theft, "_roll", spy)
    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert rolled == [90], "a one-star heist must roll 90 %"
    assert result == {"status": "success", "amount": 200, "wanted_level": 2}


async def test_wanted_level_five_rolls_fifty(session, now, monkeypatch):
    """Test 3: the rate really reaches 50 % at five stars (bug 2)."""
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=5, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    started = await theft.initiate_theft(session, 2, 1)
    assert started["status"] == "pending"

    rolled: list[int] = []

    def spy(percent: int) -> bool:
        rolled.append(percent)
        return False

    monkeypatch.setattr(theft, "_roll", spy)
    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert rolled == [50]
    assert result == {"status": "failed"}
    assert await _balance(session, 1) == 4000


async def test_level_zero_theft_settles_instantly(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    # A level-0 row with no expiry is a clean player.
    await _state(session, 2, wanted_level=0)

    result = await theft.initiate_theft(session, 2, 1)

    assert result == {"status": "success", "amount": 200, "wanted_level": 1}
    assert await _balance(session, 1) == 3800
    assert await _balance(session, 2) == 200
    state = await session.get(UserWantedState, 2)
    assert state.wanted_level == 1
    assert _aware(state.last_stolen_at) == _utc(now)
    assert _aware(state.wanted_expires_at) == _utc(now) + timedelta(hours=24)

    ledger = await _transactions(session)
    assert [(tx.user_id, tx.amount, tx.reason) for tx in ledger] == [
        (1, -200, theft.REASON_STOLEN),
        (2, 200, theft.REASON_STOLEN),
    ]


async def test_missing_state_is_created_by_the_first_theft(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)

    result = await theft.initiate_theft(session, 2, 1)

    assert result["wanted_level"] == 1
    assert await session.scalar(
        sa.select(UserWantedState.wanted_level).where(UserWantedState.user_id == 2)
    ) == 1


async def test_only_a_clean_thief_settles_instantly(session, now):
    """Regression (bug 1): one star already forces the arrest window."""
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=1, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.initiate_theft(session, 2, 1)

    assert result == {"status": "pending", "amount": 200, "window_seconds": 10}
    assert await _balance(session, 1) == 4000
    assert await _balance(session, 2) == 0
    assert (await session.get(UserWantedState, 2)).wanted_level == 1
    assert theft.INSTANT_THEFT_MAX_LEVEL == 0


@pytest.mark.parametrize(
    "balance,overrides,expected",
    [
        (4000, {}, 200),                          # 5% of 4000
        (600, {}, 50),                            # 30 clamped up to the minimum
        (100_000, {}, 500),                       # 5000 clamped to the maximum
        (50, {}, 50),                             # exactly the minimum
        (4000, {"theft_amount_percent": 10}, 400),
        (4000, {"theft_amount_percent": 200}, 500),   # percent is clamped to 100
        (10_000, {"theft_min_amount": 1000}, 1000),
        (4000, {"theft_max_amount": 25}, 50),     # a cap below the min is raised
    ],
)
async def test_theft_amount_is_clamped(session, now, balance, overrides, expected):
    await _users(session, 1, 2)
    await _wallet(session, 1, balance)
    await _wallet(session, 2, 0)
    for key, value in overrides.items():
        await _set_config(session, key, value)

    result = await theft.initiate_theft(session, 2, 1)

    assert result == {"status": "success", "amount": expected, "wanted_level": 1}
    assert await _balance(session, 1) == balance - expected


# ---------------------------------------------------------------------------
# initiate_theft: level 2+ is a pending heist
# ---------------------------------------------------------------------------

async def test_pending_theft_moves_no_money(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=2, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.initiate_theft(session, 2, 1)

    assert result == {"status": "pending", "amount": 200, "window_seconds": 10}
    assert await _balance(session, 1) == 4000
    assert await _balance(session, 2) == 0
    assert await _transactions(session) == []
    state = await session.get(UserWantedState, 2)
    assert state.wanted_level == 2
    assert state.last_stolen_at is None


async def test_arrest_window_seconds_are_configurable(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=3, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )
    await _set_config(session, "theft_arrest_window_seconds", 25)

    assert (await theft.initiate_theft(session, 2, 1))["window_seconds"] == 25


# ---------------------------------------------------------------------------
# resolve_pending_theft: arrested before the window ends
# ---------------------------------------------------------------------------

async def test_arrest_jails_thief_pays_arrester_and_spares_the_victim(session, now):
    await _users(session, 1, 2, 3)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _wallet(session, 3, 25)
    await _state(
        session, 2, wanted_level=3, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=3)

    assert result == {"status": "caught", "reward": 300}
    assert await _balance(session, 1) == 4000  # victim keeps every apple
    assert await _balance(session, 2) == 0      # thief gains nothing
    assert await _balance(session, 3) == 325    # bot-funded bounty on top
    state = await session.get(UserWantedState, 2)
    assert _aware(state.jailed_until) == _utc(now) + timedelta(hours=2)
    assert state.wanted_level == 0
    assert state.wanted_expires_at is None

    ledger = await _transactions(session)
    assert [(tx.user_id, tx.amount, tx.reason) for tx in ledger] == [
        (3, 300, theft.REASON_ARREST_REWARD),
    ]


async def test_arrest_reward_scales_with_stars_and_config(session, now):
    await _users(session, 1, 2, 3)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _wallet(session, 3, 0)
    await _state(
        session, 2, wanted_level=5, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )
    await _set_config(session, "theft_arrest_reward_per_star", 40)

    assert await theft.resolve_pending_theft(session, 2, 1, 200, 3) == {
        "status": "caught",
        "reward": 200,
    }
    assert await _balance(session, 3) == 200


async def test_arrest_with_unknown_arrester_is_refused(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=2, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    assert await theft.resolve_pending_theft(session, 2, 1, 200, 999) == {
        "status": "not_found"
    }
    assert await _balance(session, 1) == 4000
    assert (await session.get(UserWantedState, 2)).jailed_until is None


# ---------------------------------------------------------------------------
# resolve_pending_theft: nobody arrested in time
# ---------------------------------------------------------------------------

async def test_no_arrest_success_moves_money_and_levels_up(session, now, monkeypatch):
    monkeypatch.setattr(theft.random, "random", lambda: 0.0)
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=2, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert result == {"status": "success", "amount": 200, "wanted_level": 3}
    assert await _balance(session, 1) == 3800
    assert await _balance(session, 2) == 200
    state = await session.get(UserWantedState, 2)
    assert state.wanted_level == 3
    assert state.jailed_until is None
    assert _aware(state.last_stolen_at) == _utc(now)
    assert _aware(state.wanted_expires_at) == _utc(now) + timedelta(hours=24)
    assert [(tx.user_id, tx.amount) for tx in await _transactions(session)] == [
        (1, -200),
        (2, 200),
    ]


async def test_no_arrest_failed_roll_moves_nothing(session, now, monkeypatch):
    monkeypatch.setattr(theft.random, "random", lambda: 0.99)
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=2, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert result == {"status": "failed"}
    assert await _balance(session, 1) == 4000
    assert await _balance(session, 2) == 0
    assert await _transactions(session) == []
    state = await session.get(UserWantedState, 2)
    assert state.wanted_level == 2
    assert state.last_stolen_at is None


@pytest.mark.parametrize(
    "level,roll,expected",
    [
        # Bug 2: 100 % (L0) - 90 (L1) - 80 (L2) - 70 (L3) - 60 (L4) - 50 (L5).
        (1, 0.899, "success"), (1, 0.9, "failed"),
        (2, 0.799, "success"), (2, 0.8, "failed"),
        (3, 0.699, "success"), (3, 0.7, "failed"),
        (4, 0.599, "success"), (4, 0.6, "failed"),
        (5, 0.499, "success"), (5, 0.5, "failed"),
    ],
)
async def test_success_chance_drops_with_every_star(
    session, now, monkeypatch, level, roll, expected
):
    monkeypatch.setattr(theft.random, "random", lambda: roll)
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=level, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert result["status"] == expected


async def test_success_never_exceeds_five_stars(session, now, monkeypatch):
    monkeypatch.setattr(theft.random, "random", lambda: 0.0)
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=5, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )

    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert result == {"status": "success", "amount": 200, "wanted_level": 5}


async def test_success_falls_back_to_failure_when_the_victim_spent_the_apples(
    session, now, monkeypatch
):
    monkeypatch.setattr(theft.random, "random", lambda: 0.0)
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)
    await _state(
        session, 2, wanted_level=2, wanted_expires_at=_utc(now) + timedelta(hours=5)
    )
    (await session.get(AppleWallet, 1)).balance = 10
    await session.flush()

    result = await theft.resolve_pending_theft(session, 2, 1, 200, caught_by_id=None)

    assert result == {"status": "failed"}
    assert await _balance(session, 1) == 10
    assert await _balance(session, 2) == 0
    assert await _transactions(session) == []


async def test_resolution_rejects_a_non_positive_amount(session, now):
    await _users(session, 1, 2)
    await _wallet(session, 1, 4000)
    await _wallet(session, 2, 0)

    assert await theft.resolve_pending_theft(session, 2, 1, 0, None) == {
        "status": "failed"
    }
    assert await theft.resolve_pending_theft(session, 2, 1, 200, 999) == {
        "status": "not_found"
    }
