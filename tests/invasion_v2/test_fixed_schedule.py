"""Fixed daily invasion v2 spawn schedule (Asia/Tehran): 20:00 by default.

Covers the pure slot helper (``InvasionScheduler._current_slot``) exhaustively,
then integration ticks (isolated SQLite session, exactly like
`test_scheduler.py`) to prove the per-group "already spawned this slot" gate
and the no-backfill-after-downtime behaviour end to end.
"""

import os
import time
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
    Base, Group, Inv2GroupState, Inv2Invasion, Inv2Region, PlayerCard,
    SystemConfig, User,
)
from sib.services.config_store import ConfigService
from sib.services.maintenance.service import MaintenanceService
from sib.services.invasion_v2 import circuit_breaker, scheduler
from sib.services.invasion_v2.power_resolver import set_power_resolver
from sib.services.invasion_v2.scheduler import InvasionScheduler


# Tehran local 2026-10-14 20:00:00 == 2026-10-14 16:30:00 UTC (UTC+03:30).
DAILY_SLOT_TEHRAN_UTC = datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc)
# Tehran local 2026-10-14 00:00:00 == 2026-10-13 20:30:00 UTC.
MIDNIGHT_TEHRAN_UTC = datetime(2026, 10, 13, 20, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure _current_slot helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offset_minutes", [0, 5, 29])
def test_slot_fires_within_window_at_20_tehran(offset_minutes):
    now = DAILY_SLOT_TEHRAN_UTC + timedelta(minutes=offset_minutes)
    slot = InvasionScheduler._current_slot(now)
    assert slot == DAILY_SLOT_TEHRAN_UTC


@pytest.mark.parametrize("offset_minutes", [-1, 31])
def test_slot_does_not_fire_outside_window_at_20_tehran(offset_minutes):
    now = DAILY_SLOT_TEHRAN_UTC + timedelta(minutes=offset_minutes)
    assert InvasionScheduler._current_slot(now) is None


def test_midnight_slot_remains_available_as_an_explicit_custom_schedule():
    assert InvasionScheduler._current_slot(
        MIDNIGHT_TEHRAN_UTC, spawn_hours="0"
    ) == MIDNIGHT_TEHRAN_UTC
    assert InvasionScheduler._current_slot(
        MIDNIGHT_TEHRAN_UTC + timedelta(minutes=10), spawn_hours="0"
    ) == MIDNIGHT_TEHRAN_UTC


@pytest.mark.parametrize("hour", [0, 12, 16, 20])
def test_explicitly_configured_hours_trigger(hour):
    # "local hour :00:00 Tehran" -> UTC, built from the midnight anchor.
    anchor = MIDNIGHT_TEHRAN_UTC + timedelta(hours=hour)
    slot = InvasionScheduler._current_slot(anchor, spawn_hours="0,12,16,20")
    assert slot == anchor


def test_default_schedule_has_only_the_20_tehran_slot():
    assert InvasionScheduler._current_slot(DAILY_SLOT_TEHRAN_UTC) == DAILY_SLOT_TEHRAN_UTC
    for hour in (0, 12, 16):
        anchor = MIDNIGHT_TEHRAN_UTC + timedelta(hours=hour)
        assert InvasionScheduler._current_slot(anchor, spawn_hours=None) is None


@pytest.mark.parametrize("hour", [1, 11, 13, 23])
def test_non_configured_hours_never_trigger(hour):
    anchor = MIDNIGHT_TEHRAN_UTC + timedelta(hours=hour)
    assert InvasionScheduler._current_slot(anchor) is None
    assert InvasionScheduler._current_slot(anchor + timedelta(minutes=15)) is None


def test_missing_spawn_hours_config_falls_back_to_default():
    # spawn_hours=None means "use DEFAULT_INVASION_SPAWN_HOURS" (20).
    assert InvasionScheduler._current_slot(DAILY_SLOT_TEHRAN_UTC, spawn_hours=None) == DAILY_SLOT_TEHRAN_UTC
    assert InvasionScheduler._current_slot(MIDNIGHT_TEHRAN_UTC, spawn_hours=None) is None


def test_custom_spawn_hours_and_window_are_honoured():
    anchor = MIDNIGHT_TEHRAN_UTC + timedelta(hours=9)  # 09:00 Tehran
    assert InvasionScheduler._current_slot(anchor, spawn_hours="9,21") == anchor
    assert InvasionScheduler._current_slot(
        anchor + timedelta(minutes=10), spawn_hours="9,21", slot_window_minutes=5
    ) is None
    assert InvasionScheduler._current_slot(
        anchor + timedelta(minutes=10), spawn_hours="9,21", slot_window_minutes=15
    ) == anchor


def test_server_timezone_is_never_consulted(monkeypatch):
    """Force the server's own local timezone to something other than Tehran.

    ``_current_slot`` must produce identical results either way: it always
    converts explicitly via ``zoneinfo.ZoneInfo("Asia/Tehran")`` and never
    reads ``time.tzname``/the process timezone.
    """
    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        if hasattr(time, "tzset"):
            time.tzset()
        monkeypatch.setattr(time, "tzname", ("EST", "EDT"))
        assert InvasionScheduler._current_slot(DAILY_SLOT_TEHRAN_UTC) == DAILY_SLOT_TEHRAN_UTC
        assert InvasionScheduler._current_slot(
            DAILY_SLOT_TEHRAN_UTC + timedelta(minutes=31)
        ) is None
        for hour in (0, 12, 16, 20):
            anchor = MIDNIGHT_TEHRAN_UTC + timedelta(hours=hour)
            assert InvasionScheduler._current_slot(anchor, spawn_hours="0,12,16,20") == anchor
        for hour in (1, 11, 13, 23):
            anchor = MIDNIGHT_TEHRAN_UTC + timedelta(hours=hour)
            assert InvasionScheduler._current_slot(anchor, spawn_hours="0,12,16,20") is None
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        if hasattr(time, "tzset"):
            time.tzset()


# ---------------------------------------------------------------------------
# Integration: one eligible group, isolated SQLite session (as test_scheduler.py)
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    tables = [User.__table__, Group.__table__, PlayerCard.__table__, SystemConfig.__table__,
              Inv2Region.__table__, Inv2Invasion.__table__, Inv2GroupState.__table__]
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
    set_power_resolver(SimpleNamespace(average_main_card_power=AsyncMock(return_value=500.0)))
    async with sessions.begin() as session:
        session.add(Group(id=1, title="الف", is_active=True))
        session.add(Inv2Region(code="region_0", name_fa="ناحیه", controlling_faction="بی‌طرف"))
    yield sessions
    set_power_resolver(None)
    InvasionScheduler._missing_resolver_logged = False
    await engine.dispose()


async def _count(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(Inv2Invasion))


@pytest.mark.asyncio
async def test_two_ticks_in_same_slot_spawn_only_once(db):
    await InvasionScheduler.tick(now=DAILY_SLOT_TEHRAN_UTC, rng=Random(1))
    assert await _count(db) == 1
    await InvasionScheduler.tick(now=DAILY_SLOT_TEHRAN_UTC + timedelta(minutes=10), rng=Random(1))
    assert await _count(db) == 1  # still just the one spawn for this slot
    async with db() as session:
        state = await session.get(Inv2GroupState, 1)
        assert state is not None and state.last_spawn_slot is not None
        assert state.last_spawn_slot.replace(tzinfo=timezone.utc) == DAILY_SLOT_TEHRAN_UTC


@pytest.mark.asyncio
async def test_offline_across_a_slot_does_not_backfill(db):
    """After missing 20:00, a tick at 22:00 must not backfill the invasion."""
    missed_slot_window_end = DAILY_SLOT_TEHRAN_UTC + timedelta(minutes=45)  # bot still down
    back_online = DAILY_SLOT_TEHRAN_UTC + timedelta(hours=2)  # 22:00 Tehran, no spawn slot
    assert missed_slot_window_end < back_online
    await InvasionScheduler.tick(now=back_online, rng=Random(2))
    assert await _count(db) == 0
    async with db() as session:
        assert await session.get(Inv2GroupState, 1) is None


@pytest.mark.asyncio
async def test_spawns_again_at_20_tehran_the_next_day(db):
    await InvasionScheduler.tick(now=DAILY_SLOT_TEHRAN_UTC, rng=Random(3))
    assert await _count(db) == 1
    async with db.begin() as session:
        for invasion in (await session.scalars(select(Inv2Invasion))).all():
            invasion.status = "DEFEAT"
    await InvasionScheduler.tick(now=DAILY_SLOT_TEHRAN_UTC + timedelta(days=1), rng=Random(3))
    assert await _count(db) == 2


@pytest.mark.asyncio
async def test_missing_spawn_hours_db_row_uses_code_default(db):
    # No invasion_spawn_hours/invasion_slot_window_minutes row exists in
    # system_config; ConfigService.get(...) returns None and the scheduler
    # falls back to CONFIG_DEFAULTS ("20" / "30").
    async with db() as session:
        assert await ConfigService.get(session, "invasion_spawn_hours") == "20"
        assert await ConfigService.get(session, "invasion_slot_window_minutes") == "30"
    await InvasionScheduler.tick(now=DAILY_SLOT_TEHRAN_UTC, rng=Random(5))
    assert await _count(db) == 1
