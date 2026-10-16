"""IV-1: one scheduler tick spawns an invasion for every eligible group.

Previously the scheduler spawned at most one invasion per global interval
(round-robin across groups), so with 7 groups and a 2h interval a group saw
an invasion only every ~14h. These tests pin the new policy: the global
interval gate still stops a second tick inside the same interval, but a
single due tick iterates all active groups in id order and spawns for each
one that is unmuted, below `invasion_max_active_per_group` live invasions
and resolved to a positive average card power.

No Telegram or external database is contacted; the session boundary used by
production is replaced by an isolated SQLite-backed fake provider, exactly
like `test_scheduler.py`.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from random import Random
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import (
    Base, Group, User, PlayerCard, SystemConfig,
    Inv2Attack, Inv2GroupState, Inv2Invasion, Inv2Region, Inv2Reward,
)
from sib.services.config_store import ConfigService
from sib.services.maintenance.service import MaintenanceService
from sib.services.invasion_v2 import circuit_breaker, scheduler
from sib.services.invasion_v2.enums import EnemyType, InvasionStatus
from sib.services.invasion_v2.power_resolver import set_power_resolver
from sib.services.invasion_v2.scheduler import InvasionScheduler


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


class FakeResolver:
    """Per-group power map; a missing/None entry means unknown power."""

    def __init__(self, powers: dict[int, float | None]):
        self.powers = powers
        self.calls: list[int] = []

    async def average_main_card_power(self, *, group_id: int, session) -> float | None:
        self.calls.append(group_id)
        return self.powers.get(group_id)


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    tables = [User.__table__, Group.__table__, PlayerCard.__table__, SystemConfig.__table__,
              Inv2Region.__table__, Inv2Invasion.__table__, Inv2Attack.__table__,
              Inv2Reward.__table__, Inv2GroupState.__table__]
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
    set_power_resolver(None)
    async with sessions.begin() as session:
        session.add_all([Group(id=1, title="الف", is_active=True),
                         Group(id=2, title="ب", is_active=True),
                         Group(id=3, title="ج", is_active=True)])
        session.add_all([Inv2Region(code=f"region_{n}", name_fa=f"ناحیه {n}",
                                    controlling_faction="بی‌طرف") for n in range(10)])
    yield sessions
    set_power_resolver(None)
    InvasionScheduler._missing_resolver_logged = False
    await engine.dispose()


async def _rows(sessions) -> list[Inv2Invasion]:
    async with sessions() as session:
        return list((await session.scalars(
            select(Inv2Invasion).order_by(Inv2Invasion.group_id, Inv2Invasion.id))).all())


def _tick_record(caplog) -> logging.LogRecord:
    records = [r for r in caplog.records if r.msg == "inv2_tick"]
    assert records, "expected a structured inv2_tick log record"
    return records[-1]


async def _add_live_invasion(sessions, group_id: int, key: str, *,
                             status: str = "ACTIVE",
                             scheduled_at: datetime | None = None) -> None:
    """A live invasion old enough not to trip the global interval gate."""
    async with sessions.begin() as session:
        session.add(Inv2Invasion(
            group_id=group_id, region_code="region_0", enemy_type="soldier",
            enemy_name_fa="شیاسیب پیاده", enemy_hp=100, enemy_hp_remaining=100,
            difficulty_multiplier=2.5, status=status,
            scheduled_at=scheduled_at or (NOW - timedelta(hours=2)),
            started_at=NOW - timedelta(hours=2) if status == "ACTIVE" else None,
            ends_at=NOW + timedelta(minutes=30) if status == "ACTIVE" else None,
            occurrence_key=key))


@pytest.mark.asyncio
async def test_spawn_all_eligible_groups_in_one_tick(db):
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    assert len(rows) == 3
    assert [row.group_id for row in rows] == [1, 2, 3]
    assert all(row.status == InvasionStatus.PENDING.value for row in rows)
    assert all(row.enemy_hp > 0 for row in rows)


@pytest.mark.asyncio
async def test_skip_groups_with_insufficient_players(db, caplog):
    # Group 2 resolves to None (e.g. fewer than
    # invasion_min_players_with_card members own a Main Card).
    set_power_resolver(FakeResolver({1: 500.0, 2: None, 3: 500.0}))
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    assert [row.group_id for row in rows] == [1, 3]
    record = _tick_record(caplog)
    assert record.spawned == 2
    assert record.skipped_insufficient_players == 1


@pytest.mark.asyncio
async def test_skip_muted_groups(db, caplog):
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    for _ in range(3):  # circuit-breaker threshold: mute group 1 for 24h
        await circuit_breaker.record_send_failure(1, now=NOW)
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    assert [row.group_id for row in rows] == [2, 3]
    record = _tick_record(caplog)
    assert record.spawned == 2
    assert record.skipped_muted == 1


@pytest.mark.asyncio
async def test_skip_groups_with_active_invasion(db, caplog):
    await _add_live_invasion(db, 2, "pre-existing-active")
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    # Group 2 keeps only its pre-existing invasion; 1 and 3 spawn fresh ones.
    assert [row.group_id for row in rows] == [1, 2, 3]
    assert [row.occurrence_key for row in rows if row.group_id == 2] == ["pre-existing-active"]
    record = _tick_record(caplog)
    assert record.spawned == 2
    assert record.skipped_active == 1


@pytest.mark.asyncio
async def test_no_spawn_before_interval_elapsed(db, caplog):
    # Most recent invasion anywhere was 10 minutes ago; the default global
    # interval is 3600s, so this tick must not spawn for any group.
    await _add_live_invasion(db, 1, "recent", status="PENDING",
                             scheduled_at=NOW - timedelta(minutes=10))
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    assert len(rows) == 1
    assert _tick_record(caplog).spawned == 0


@pytest.mark.asyncio
async def test_respects_max_active_per_group(db, caplog):
    async with db.begin() as session:
        await ConfigService.set(session, "invasion_max_active_per_group", 2)
    await _add_live_invasion(db, 1, "g1-live-a")
    await _add_live_invasion(db, 1, "g1-live-b")  # group 1 already at the cap
    await _add_live_invasion(db, 2, "g2-live-a")  # group 2 has room for one more
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    counts = {gid: sum(1 for row in rows if row.group_id == gid) for gid in (1, 2, 3)}
    assert counts == {1: 2, 2: 2, 3: 1}  # only groups 2 and 3 gained one
    record = _tick_record(caplog)
    assert record.spawned == 2
    assert record.skipped_active == 1


@pytest.mark.asyncio
async def test_spawn_count_can_exceed_one(db, caplog):
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    record = _tick_record(caplog)
    stats = {name: getattr(record, name) for name in (
        "spawned", "skipped_muted", "skipped_active",
        "skipped_no_resolver", "skipped_insufficient_players")}
    assert stats["spawned"] > 1
    assert stats["spawned"] == 3


@pytest.mark.asyncio
async def test_per_group_spawn_log_line(db, caplog, monkeypatch):
    monkeypatch.setattr(scheduler, "choose_enemy_type", lambda rng: EnemyType.SOLDIER)
    set_power_resolver(FakeResolver({1: 500.0, 2: 500.0, 3: 500.0}))
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(4))
    rows = await _rows(db)
    assert len(rows) == 3
    for row in rows:
        expected = (f"inv2 spawned: group={row.group_id} enemy={row.enemy_type} "
                    f"hp={row.enemy_hp} region={row.region_code}")
        assert expected in caplog.text
    # SOLDIER x1.0 at the default difficulty 2.5 over average power 500.
    assert "inv2 spawned: group=1 enemy=soldier hp=1250 region=" in caplog.text
