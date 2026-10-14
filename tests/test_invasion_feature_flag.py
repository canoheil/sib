"""DB-free checks for the invasion rework switch and its user-facing gate."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sib.services.maintenance import service as maintenance_module
from sib.services.maintenance.service import MaintenanceService


@pytest.fixture
def flag_reader(monkeypatch):
    clock = [1000.0]
    read = AsyncMock(return_value=None)

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr(maintenance_module, "get_session", fake_session)
    monkeypatch.setattr(maintenance_module.ConfigService, "get_raw", read)
    monkeypatch.setattr(maintenance_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(maintenance_module, "_invasion_flag_value", False)
    monkeypatch.setattr(maintenance_module, "_invasion_flag_expires_at", 0.0)
    monkeypatch.setattr(maintenance_module, "_invasion_flag_lock", asyncio.Lock())
    return clock, read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("anything", False),
        ('"false"', False),
        ("true", True),
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
        ('"yes"', True),
    ],
)
async def test_invasion_flag_value_and_60_second_cache(flag_reader, raw, expected):
    clock, read = flag_reader
    read.return_value = raw
    assert await MaintenanceService._invasion_enabled() is expected
    read.return_value = "false" if expected else "true"
    clock[0] += 59
    assert await MaintenanceService._invasion_enabled() is expected
    read.assert_awaited_once()
    clock[0] += 1
    assert await MaintenanceService._invasion_enabled() is (not expected)
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_invasion_flag_read_failure_fails_closed_and_is_cached(flag_reader):
    clock, read = flag_reader
    read.side_effect = RuntimeError("config unavailable")
    assert await MaintenanceService._invasion_enabled() is False
    assert await MaintenanceService._invasion_enabled() is False
    read.assert_awaited_once()
    clock[0] += 60
    read.side_effect = None
    read.return_value = "true"
    assert await MaintenanceService._invasion_enabled() is True


@pytest.mark.asyncio
async def test_disabled_inv2_join_callback_does_not_read_invasion(monkeypatch):
    from sib.handlers import inv2_join

    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    answer = AsyncMock()
    callback = SimpleNamespace(data="inv2:join:123", message=None, from_user=None, answer=answer)
    await inv2_join.handle_inv2_join(callback, bot=None)
    answer.assert_awaited_once_with(text="⚔️ سیستم تهاجم موقتاً غیرفعال است.", show_alert=True)


@pytest.mark.asyncio
async def test_disabled_tick_does_not_open_session(monkeypatch):
    from sib.services.invasion_v2 import scheduler

    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    def unexpected_session():
        raise AssertionError("disabled tick opened a database session")
    monkeypatch.setattr(scheduler, "get_session", unexpected_session)
    assert await scheduler.InvasionScheduler.tick(now=datetime.now(timezone.utc)) is None
