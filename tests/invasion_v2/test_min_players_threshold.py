"""A6: a group spawns only when enough active members own a Main Card.

The policy replaces the old fail-closed resolver rule (one member without a
card blocked the whole group) with a minimum-threshold rule: members without a
card are ignored, and the average covers only the members who have one.

Everything else stays: the activity window, the DB-error safety net and the
"no resolver configured" safety net all still return None / skip the group.
"""

import importlib.util
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import (
    Base, Character, Group, Inv2GroupMember, Inv2GroupState, Inv2Invasion,
    Inv2Region, PlayerCard, SystemConfig, User,
)
from sib.services.config_store import CONFIG_DEFAULTS, DEFAULT_INVASION_DIFFICULTY_MULTIPLIER
from sib.services.invasion_v2 import circuit_breaker, scheduler
from sib.services.invasion_v2.enemy import compute_enemy_hp
from sib.services.invasion_v2.enums import EnemyType
from sib.services.invasion_v2.power_resolver import set_power_resolver
from sib.services.invasion_v2.power_resolver_impl import DbGroupPowerResolver
from sib.services.invasion_v2.scheduler import InvasionScheduler
from sib.services.maintenance.service import MaintenanceService

MIN_KEY = "invasion_min_players_with_card"
NOW = datetime.now(timezone.utc)

# One shared character: level 1 -> power 20, every further level adds 10.
CHARACTER_ID = 10
GROUP_ID = -100
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "database/migrations/versions/046_inv2_min_players_with_card.py"
)


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
async def sessions():
    """Isolated SQLite database with everything the resolver reads."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    tables = [User.__table__, Group.__table__, Character.__table__,
              PlayerCard.__table__, Inv2GroupMember.__table__, SystemConfig.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(Group(id=GROUP_ID, title="گروه"))
        session.add(Character(id=CHARACTER_ID, code="test", name_fa="تست",
                              power=20, speed=20, intelligence=20))
        session.add_all([User(id=n, first_name=f"User{n}") for n in range(1, 11)])
    yield factory
    await engine.dispose()


async def observe(sessions, user_id: int, seen_at=None) -> None:
    """Mark a user as recently seen in the group; no card yet."""
    async with sessions.begin() as session:
        session.add(Inv2GroupMember(user_id=user_id, group_id=GROUP_ID,
                                    last_seen_at=seen_at or NOW))


async def select_main(sessions, user_id: int, level: int) -> None:
    """Give a user a Main Card of the shared character at ``level``."""
    async with sessions.begin() as session:
        session.add(PlayerCard(id=user_id * 100, user_id=user_id,
                               character_id=CHARACTER_ID, level=level, is_favorite=True))


async def set_threshold(sessions, value: str) -> None:
    async with sessions.begin() as session:
        row = await session.get(SystemConfig, MIN_KEY)
        if row is None:
            session.add(SystemConfig(key=MIN_KEY, value=value))
        else:
            row.value = value


async def clear_threshold(sessions) -> None:
    async with sessions.begin() as session:
        row = await session.get(SystemConfig, MIN_KEY)
        if row is not None:
            await session.delete(row)


# --------------------------------------------------------------------------- #
# Resolver policy
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolver_below_threshold_returns_none(sessions, caplog):
    for user_id in (1, 2):
        await observe(sessions, user_id)
        await select_main(sessions, user_id, 2)  # 30 each, but only 2 players
    resolver = DbGroupPowerResolver()
    with caplog.at_level(logging.INFO, logger="sib"):
        async with sessions() as session:
            assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    assert "inv2 spawn skipped: group=-100 players_with_card=2 required=3" in caplog.text


@pytest.mark.asyncio
async def test_resolver_at_threshold_returns_average(sessions):
    for user_id, level in ((1, 1), (2, 2), (3, 3)):  # 20, 30, 40 -> 30.0
        await observe(sessions, user_id)
        await select_main(sessions, user_id, level)
    resolver = DbGroupPowerResolver()
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 30.0
    assert CONFIG_DEFAULTS[MIN_KEY] == 3  # no config row: this is the default


@pytest.mark.asyncio
async def test_resolver_above_threshold_ignores_without_card(sessions):
    for user_id, level in ((1, 1), (2, 2), (3, 3)):
        await observe(sessions, user_id)
        await select_main(sessions, user_id, level)
    for user_id in range(4, 9):  # five members inside the window, no card
        await observe(sessions, user_id)
    resolver = DbGroupPowerResolver()
    async with sessions() as session:
        # Average over the three card owners only, not over all eight members.
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 30.0


@pytest.mark.asyncio
async def test_resolver_zero_with_card_returns_none(sessions, caplog):
    for user_id in (1, 2, 3, 4):
        await observe(sessions, user_id)
    resolver = DbGroupPowerResolver()
    with caplog.at_level(logging.INFO, logger="sib"):
        async with sessions() as session:
            assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    assert "players_with_card=0 required=3" in caplog.text


@pytest.mark.asyncio
async def test_threshold_reads_from_config(sessions):
    for user_id, level in ((1, 1), (2, 3)):  # 20 and 40 -> 30.0
        await observe(sessions, user_id)
        await select_main(sessions, user_id, level)
    resolver = DbGroupPowerResolver()
    await set_threshold(sessions, "1")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 30.0
    await set_threshold(sessions, "2")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 30.0
    await set_threshold(sessions, "3")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    await set_threshold(sessions, "10")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    # An unusable threshold is a configuration error: no spawn, never a guess.
    await set_threshold(sessions, "0")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    await set_threshold(sessions, "many")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None


@pytest.mark.asyncio
async def test_missing_config_key_uses_default_3(sessions):
    for user_id in (1, 2):
        await observe(sessions, user_id)
        await select_main(sessions, user_id, 1)
    resolver = DbGroupPowerResolver()
    async with sessions() as session:
        assert await session.scalar(select(SystemConfig).where(SystemConfig.key == MIN_KEY)) is None
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    await observe(sessions, 3)
    await select_main(sessions, 3, 1)
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 20.0
    # Seeding the key with the same value changes nothing.
    await set_threshold(sessions, "3")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 20.0
    await clear_threshold(sessions)
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 20.0


@pytest.mark.asyncio
async def test_ignored_members_are_logged_at_debug(sessions, caplog):
    for user_id in (1, 2, 3):
        await observe(sessions, user_id)
        await select_main(sessions, user_id, 1)
    for user_id in (4, 5, 6):
        await observe(sessions, user_id)
    resolver = DbGroupPowerResolver()
    with caplog.at_level(logging.DEBUG, logger="sib"):
        async with sessions() as session:
            assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 20.0
    debug = [r for r in caplog.records
             if r.levelno == logging.DEBUG and "ignoring members without main card" in r.getMessage()]
    assert len(debug) == 1
    assert debug[0].getMessage() == (
        "inv2 group power ignoring members without main card group=-100 ignored=[4, 5, 6]")
    # Nothing is logged when every member of the window has a card.
    caplog.clear()
    for user_id in (4, 5, 6):
        await select_main(sessions, user_id, 1)
    with caplog.at_level(logging.DEBUG, logger="sib"):
        async with sessions() as session:
            assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) == 20.0
    assert "ignoring members without main card" not in caplog.text


@pytest.mark.asyncio
async def test_db_error_still_returns_none(sessions, monkeypatch, caplog):
    """Unchanged safety net: any DB failure resolves to None, never a guess."""
    from sib.services.invasion_v2 import power_resolver_impl

    await observe(sessions, 1)
    await select_main(sessions, 1, 1)
    resolver = DbGroupPowerResolver()
    monkeypatch.setattr(power_resolver_impl.ConfigService, "get",
                        AsyncMock(side_effect=RuntimeError("DB down")))
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    # A failure while reading a card is caught by the same net.
    monkeypatch.setattr(power_resolver_impl.PlayerCardService, "get_main_card",
                        AsyncMock(side_effect=RuntimeError("card read failed")))
    with caplog.at_level(logging.DEBUG, logger="sib"):
        async with sessions() as session:
            assert await resolver.average_main_card_power(group_id=GROUP_ID, session=session) is None
    assert "inv2 group power unresolved group=-100" in caplog.text


# --------------------------------------------------------------------------- #
# Scheduler integration (no scheduler code changed by A6)
# --------------------------------------------------------------------------- #
@pytest.fixture
async def scheduler_db(monkeypatch):
    """SQLite DB wired to the real scheduler and the real DbGroupPowerResolver."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    tables = [User.__table__, Group.__table__, Character.__table__, PlayerCard.__table__,
              Inv2GroupMember.__table__, SystemConfig.__table__, Inv2Region.__table__,
              Inv2Invasion.__table__, Inv2GroupState.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def fake_get_session():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(scheduler, "get_session", fake_get_session)
    monkeypatch.setattr(circuit_breaker, "get_session", fake_get_session)
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=True))
    InvasionScheduler._missing_resolver_logged = False
    set_power_resolver(DbGroupPowerResolver())
    async with sessions.begin() as session:
        session.add(Group(id=1, title="الف", is_active=True))
        session.add(Character(id=CHARACTER_ID, code="test", name_fa="تست",
                              power=20, speed=20, intelligence=20))
        session.add_all([User(id=n, first_name=f"User{n}") for n in range(1, 11)])
        session.add_all([Inv2Region(code=f"region_{n}", name_fa=f"ناحیه {n}",
                                    controlling_faction="بی‌طرف") for n in range(10)])
    yield sessions
    set_power_resolver(None)
    InvasionScheduler._missing_resolver_logged = False
    await engine.dispose()


async def _member(sessions, user_id: int, level: int | None) -> None:
    """Observe a member of group 1, optionally with a Main Card at ``level``."""
    async with sessions.begin() as session:
        session.add(Inv2GroupMember(user_id=user_id, group_id=1, last_seen_at=NOW))
        if level is not None:
            session.add(PlayerCard(id=user_id * 100, user_id=user_id,
                                   character_id=CHARACTER_ID, level=level, is_favorite=True))


@pytest.mark.asyncio
async def test_scheduler_skips_group_with_insufficient_players(scheduler_db, caplog):
    await _member(scheduler_db, 1, 1)
    await _member(scheduler_db, 2, 1)  # two card owners, five hangers-on
    for user_id in range(3, 8):
        await _member(scheduler_db, user_id, None)
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(7))
    assert "inv2 spawn skipped: group=1 players_with_card=2 required=3" in caplog.text
    async with scheduler_db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Invasion)) == 0


@pytest.mark.asyncio
async def test_scheduler_spawns_group_with_enough_players(scheduler_db, caplog):
    await _member(scheduler_db, 1, 1)
    await _member(scheduler_db, 2, 2)
    await _member(scheduler_db, 3, 3)  # 20, 30, 40 -> average 30
    for user_id in range(4, 9):
        await _member(scheduler_db, user_id, None)  # ignored, not fatal
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(7))
    assert "inv2 spawn skipped: group=1 players_with_card=" not in caplog.text
    records = [r for r in caplog.records if r.msg == "inv2_tick"]
    assert records and records[-1].spawned == 1
    async with scheduler_db() as session:
        rows = list((await session.scalars(select(Inv2Invasion))).all())
    assert len(rows) == 1
    assert rows[0].group_id == 1
    # HP still comes from the unchanged formula over the three card owners:
    # an average of exactly 30.0, not 30 x 3/8 (the naive "all members" read).
    assert rows[0].enemy_hp == compute_enemy_hp(
        30.0, EnemyType(rows[0].enemy_type), DEFAULT_INVASION_DIFFICULTY_MULTIPLIER)
    assert rows[0].enemy_hp > 0


# --------------------------------------------------------------------------- #
# Migration 046
# --------------------------------------------------------------------------- #
def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_046", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_migration_046_seeds_key_and_downgrade_removes_it():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    migration = _load_migration()
    assert migration.revision == "046_inv2_min_players_with_card"
    assert migration.down_revision == "045_group_welcomed_at"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[SystemConfig.__table__])
        conn.execute(SystemConfig.__table__.insert(),
                     {"key": "invasion_enabled", "value": "false"})
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == MIN_KEY)) == "3"
        assert conn.scalar(select(SystemConfig.value).where(
            SystemConfig.key == "invasion_enabled")) == "false"
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == MIN_KEY)) is None
        assert conn.scalar(select(SystemConfig.value).where(
            SystemConfig.key == "invasion_enabled")) == "false"
    engine.dispose()


def test_migration_046_preserves_a_customized_value():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[SystemConfig.__table__])
        conn.execute(SystemConfig.__table__.insert(), {"key": MIN_KEY, "value": "7"})
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()   # existing admin value wins
            migration.downgrade()  # ... and is never destroyed by the rollback
        assert conn.scalar(select(SystemConfig.value).where(SystemConfig.key == MIN_KEY)) == "7"
    engine.dispose()


def test_migration_046_quotes_reserved_columns():
    text = MIGRATION_PATH.read_text()
    assert "`key`" in text and "`value`" in text  # migration 040's bug
    assert "ON DUPLICATE KEY UPDATE `key` = `key`" in text
    assert "ON CONFLICT (`key`) DO NOTHING" in text


def test_migration_046_rejects_unsupported_dialect(monkeypatch):
    migration = _load_migration()

    class _FakeOp:
        @staticmethod
        def get_bind():
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        @staticmethod
        def execute(_statement):
            raise AssertionError("no statement may run on an unsupported dialect")

    monkeypatch.setattr(migration, "op", _FakeOp)
    with pytest.raises(RuntimeError, match="Unsupported inv2 min-players seed dialect"):
        migration.upgrade()
