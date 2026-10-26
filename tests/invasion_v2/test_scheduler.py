"""Inv2 scheduler/circuit integration with an isolated, injected async session.

No Telegram or external database is contacted. The session boundary used by
production is replaced by this SQLite-backed fake provider.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import (
    Base, Group, User, PlayerCard, SystemConfig,
    Inv2Attack, Inv2GroupState, Inv2Invasion, Inv2Region, Inv2Reward,
)
from sib.services.maintenance.service import MaintenanceService
from sib.services.invasion_v2 import circuit_breaker, scheduler
from sib.services.invasion_v2.enums import EnemyType, InvasionStatus
from sib.services.invasion_v2.power_resolver import set_power_resolver
from sib.services.invasion_v2.scheduler import InvasionScheduler


NOW = datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc)  # Tehran local 20:00:00 (the daily spawn slot)


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
                         Group(id=3, title="خاموش", is_active=False)])
        session.add_all([Inv2Region(code=f"region_{n}", name_fa=f"ناحیه {n}",
                                    controlling_faction="بی‌طرف") for n in range(10)])
    yield sessions
    set_power_resolver(None)
    InvasionScheduler._missing_resolver_logged = False
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_flag_prevents_any_session_or_write(monkeypatch):
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))

    @asynccontextmanager
    async def forbidden():
        raise AssertionError("disabled feature opened a DB session")
        yield

    monkeypatch.setattr(scheduler, "get_session", forbidden)
    monkeypatch.setattr(circuit_breaker, "get_session", forbidden)
    assert await InvasionScheduler.tick(now=NOW) is None
    assert await InvasionScheduler.recover(now=NOW) == 0
    await circuit_breaker.record_send_failure(1, now=NOW)
    await circuit_breaker.record_send_success(1)
    assert not await circuit_breaker.is_group_available(1, now=NOW)
    assert not await circuit_breaker.maybe_permanently_disable(1, now=NOW)


@pytest.mark.asyncio
async def test_no_resolver_skips_spawn_and_logs_only_once(db, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="sib"):
        await InvasionScheduler.tick(now=NOW, rng=Random(3))
        await InvasionScheduler.tick(now=NOW + timedelta(seconds=5), rng=Random(3))
    assert caplog.text.count("inv2 spawn skipped: no power resolver configured") == 1
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Invasion)) == 0


@pytest.mark.asyncio
async def test_unknown_power_skips_spawn(db):
    resolver = SimpleNamespace(average_main_card_power=AsyncMock(return_value=None))
    set_power_resolver(resolver)
    await InvasionScheduler.tick(now=NOW, rng=Random(3))
    assert resolver.average_main_card_power.await_count == 2  # both active groups
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Invasion)) == 0


@pytest.mark.asyncio
async def test_resolved_power_spawns_for_each_eligible_group_with_formula(db, monkeypatch):
    # IV-1: one tick spawns for every eligible group (previously round-robin
    # spawned for group 1 only and group 2 waited for a later interval).
    resolver = SimpleNamespace(average_main_card_power=AsyncMock(return_value=500.0))
    set_power_resolver(resolver)
    monkeypatch.setattr(scheduler, "choose_enemy_type", lambda rng: EnemyType.SHADOW)
    await InvasionScheduler.tick(now=NOW, rng=Random(4))
    await InvasionScheduler.tick(now=NOW + timedelta(seconds=5), rng=Random(4))
    async with db() as session:
        rows = list((await session.scalars(select(Inv2Invasion).order_by(Inv2Invasion.id))).all())
        assert len(rows) == 2
        assert [row.group_id for row in rows] == [1, 2]
        first = rows[0]
        assert first.enemy_hp == first.enemy_hp_remaining == round(500 * 1.6 * 2.0)
        assert first.difficulty_multiplier == 2.0
        assert first.status == InvasionStatus.PENDING.value
        assert first.ends_at is None and first.started_at is None  # Phase 5 sends first
        assert first.occurrence_key.startswith("inv2:v1:group:1:region:")
        assert (await session.get(Inv2Region, first.region_code)).last_invasion_id == first.id
    # At the next daily 20:00 slots, both groups still hold a live PENDING
    # invasion, so the active-invasion guard prevents another spawn.
    await InvasionScheduler.tick(now=NOW + timedelta(days=1, seconds=1), rng=Random(6))
    await InvasionScheduler.tick(now=NOW + timedelta(days=2, seconds=2), rng=Random(6))
    async with db() as session:
        rows = list((await session.scalars(select(Inv2Invasion).order_by(Inv2Invasion.id))).all())
        assert [row.group_id for row in rows] == [1, 2]  # no overlapping PENDING
    assert resolver.average_main_card_power.await_count == 2


@pytest.mark.asyncio
async def test_muted_and_inactive_groups_are_skipped_during_spawn(db, monkeypatch):
    set_power_resolver(SimpleNamespace(average_main_card_power=AsyncMock(return_value=500.0)))
    monkeypatch.setattr(scheduler, "choose_enemy_type", lambda rng: EnemyType.SOLDIER)
    for _ in range(3):
        await circuit_breaker.record_send_failure(1, now=NOW)
    # Same slot the mute was recorded in: group 1 stays muted (24h mute),
    # group 2 is free to spawn.
    await InvasionScheduler.tick(now=NOW, rng=Random(12))
    async with db() as session:
        rows = list((await session.scalars(select(Inv2Invasion))).all())
        assert len(rows) == 1 and rows[0].group_id == 2


def test_region_selection_seventy_thirty_with_lost_preference():
    regions = [SimpleNamespace(code=f"r{i}", is_lost_to_enemy=i == 2) for i in range(10)]
    rng = Random(45)
    chosen = [InvasionScheduler.choose_region(regions, "r1", rng).code for _ in range(1000)]
    # Lost is selected by the 70% branch AND occasionally by the 30% all-region branch.
    assert 690 <= chosen.count("r2") <= 780
    assert 0 < chosen.count("r1") < 70
    for region in regions:
        assert region.code in chosen


def test_region_selection_current_and_first_spawn_fallback():
    regions = [SimpleNamespace(code=f"r{i}", is_lost_to_enemy=False) for i in range(10)]
    chosen = [InvasionScheduler.choose_region(regions, "r1", Random(i)).code for i in range(1000)]
    assert 690 <= chosen.count("r1") <= 780
    assert InvasionScheduler.choose_region(regions, None, Random(1)) in regions
    with pytest.raises(ValueError, match="No inv2 regions"):
        InvasionScheduler.choose_region([], None, Random(1))


@pytest.mark.asyncio
async def test_circuit_three_failures_mute_then_success_resets(db):
    for count in range(1, 4):
        await circuit_breaker.record_send_failure(1, now=NOW + timedelta(minutes=count))
        async with db() as session:
            state = await session.get(Inv2GroupState, 1)
            assert state.consecutive_failures == count
            assert (state.muted_until is not None) == (count == 3)
    assert not await circuit_breaker.is_group_available(1, now=NOW + timedelta(hours=1))
    assert await circuit_breaker.is_group_available(1, now=NOW + timedelta(days=2))
    await circuit_breaker.record_send_success(1)
    async with db() as session:
        state = await session.get(Inv2GroupState, 1)
        assert state.consecutive_failures == 0 and state.muted_until is None
        assert state.last_failure_at is not None
    assert await circuit_breaker.is_group_available(1, now=NOW + timedelta(hours=1))
    assert not await circuit_breaker.is_group_available(3, now=NOW)
    assert not await circuit_breaker.is_group_available(999, now=NOW)


@pytest.mark.asyncio
async def test_circuit_disables_after_seven_days_past_mute(db):
    for _ in range(3):
        await circuit_breaker.record_send_failure(1, now=NOW)
    assert not await circuit_breaker.maybe_permanently_disable(1, now=NOW + timedelta(days=8))
    assert await circuit_breaker.maybe_permanently_disable(1, now=NOW + timedelta(days=8, seconds=1))
    assert not await circuit_breaker.maybe_permanently_disable(1, now=NOW + timedelta(days=9))
    assert not await circuit_breaker.is_group_available(1, now=NOW + timedelta(days=9))
    async with db() as session:
        assert not (await session.get(Group, 1)).is_active


@pytest.mark.asyncio
async def test_regular_tick_finalizes_defeat_and_victory_without_rewards(db):
    async with db.begin() as session:
        region_a = await session.get(Inv2Region, "region_0")
        region_b = await session.get(Inv2Region, "region_1")
        region_b.is_lost_to_enemy = True
        session.add_all([
            Inv2Invasion(group_id=1, region_code=region_a.code, enemy_type="soldier",
                         enemy_name_fa="شیاسیب پیاده", enemy_hp=30, enemy_hp_remaining=10,
                         difficulty_multiplier=4, status="ACTIVE", scheduled_at=NOW - timedelta(hours=2),
                         ends_at=NOW - timedelta(seconds=1), occurrence_key="old-a"),
            Inv2Invasion(group_id=2, region_code=region_b.code, enemy_type="shadow",
                         enemy_name_fa="شیاسیب سایه", enemy_hp=30, enemy_hp_remaining=0,
                         difficulty_multiplier=4, status="ACTIVE", scheduled_at=NOW - timedelta(hours=1),
                         ends_at=NOW - timedelta(seconds=1), occurrence_key="old-b"),
        ])
    await InvasionScheduler.tick(now=NOW, rng=Random(2))
    async with db() as session:
        rows = list((await session.scalars(select(Inv2Invasion).order_by(Inv2Invasion.id))).all())
        assert [r.status for r in rows] == ["DEFEAT", "VICTORY"]
        assert all(r.finalized_at is not None for r in rows)
        assert (await session.get(Inv2Region, "region_0")).is_lost_to_enemy
        assert not (await session.get(Inv2Region, "region_1")).is_lost_to_enemy
        assert await session.scalar(select(func.count()).select_from(Inv2Reward)) == 0


@pytest.mark.asyncio
async def test_recovery_expires_only_overdue_active_with_warning(db, caplog):
    async with db.begin() as session:
        session.add_all([
            Inv2Invasion(group_id=1, region_code="region_0", enemy_type="soldier", enemy_name_fa="شیاسیب پیاده",
                         enemy_hp=50, enemy_hp_remaining=20, difficulty_multiplier=4,
                         status="ACTIVE", scheduled_at=NOW - timedelta(hours=1),
                         ends_at=NOW - timedelta(seconds=1), occurrence_key="expired"),
            Inv2Invasion(group_id=2, region_code="region_1", enemy_type="soldier", enemy_name_fa="شیاسیب پیاده",
                         enemy_hp=50, enemy_hp_remaining=20, difficulty_multiplier=4,
                         status="ACTIVE", scheduled_at=NOW, ends_at=NOW + timedelta(minutes=1),
                         occurrence_key="future"),
        ])
    assert await InvasionScheduler.recover(now=NOW) == 1
    assert "inv2 startup recovery expired 1 overdue invasion(s)" in caplog.text
    assert await InvasionScheduler.recover(now=NOW) == 0
    async with db() as session:
        assert (await session.scalar(select(Inv2Invasion).where(Inv2Invasion.occurrence_key == "expired"))).status == "EXPIRED"
        assert (await session.scalar(select(Inv2Invasion).where(Inv2Invasion.occurrence_key == "future"))).status == "ACTIVE"
        assert not (await session.get(Inv2Region, "region_0")).is_lost_to_enemy
        assert await session.scalar(select(func.count()).select_from(Inv2Reward)) == 0


@pytest.mark.asyncio
async def test_fixed_slot_blocks_same_slot_repeat_but_allows_next_day(db):
    # Replaces the old "wait invasion_interval_seconds" cadence test: the
    # scheduler gates on the fixed daily 20:00 Asia/Tehran slot, not elapsed
    # seconds since the last spawn. NOW is the 20:00 Tehran slot.
    set_power_resolver(SimpleNamespace(average_main_card_power=AsyncMock(return_value=500.0)))
    await InvasionScheduler.tick(now=NOW, rng=Random(4))
    async with db.begin() as session:
        for invasion in (await session.scalars(select(Inv2Invasion))).all():
            invasion.status = InvasionStatus.DEFEAT.value
    # Still inside the 20:00 slot window (20 minutes later): do not spawn
    # again, even though both prior invasions have already finalized.
    await InvasionScheduler.tick(now=NOW + timedelta(minutes=20), rng=Random(4))
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Invasion)) == 2
    # At 20:00 the next day, both groups are due again.
    await InvasionScheduler.tick(now=NOW + timedelta(days=1), rng=Random(4))
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Invasion)) == 4
