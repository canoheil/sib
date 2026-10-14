"""DB-free checks for the invasion rework switch and its user-facing gate."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sib.handlers import invasion as invasion_handler
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
async def test_disabled_callback_does_not_enter_invasion_flow(monkeypatch):
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    answer = AsyncMock()
    monkeypatch.setattr(invasion_handler, "safe_answer_callback", answer)
    callback = SimpleNamespace(data="invasion:join:123")
    await invasion_handler.handle_invasion_callback(callback, bot=None)
    answer.assert_awaited_once_with(
        callback, invasion_handler.INVASION_REWORK_TEXT, show_alert=True
    )


@pytest.mark.asyncio
async def test_disabled_cycle_does_not_schedule_even_if_called_directly(monkeypatch):
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    schedule = AsyncMock()
    monkeypatch.setattr(MaintenanceService, "_schedule_current_invasions", schedule)
    monkeypatch.setattr(MaintenanceService, "_candidate_invasions", AsyncMock(return_value=[]))
    monkeypatch.setattr(MaintenanceService, "_settle_pending_rewards", AsyncMock(return_value=(0, 0)))
    await MaintenanceService.run_invasion_cycle(now=datetime.now(timezone.utc))
    schedule.assert_not_awaited()
