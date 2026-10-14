"""One structured V2 tick record and process-local health observations."""

import logging
from unittest.mock import AsyncMock

import pytest

from sib.services.invasion_v2 import admin_runtime
from sib.services.invasion_v2.scheduler import InvasionScheduler
from sib.services.maintenance.service import MaintenanceService


@pytest.mark.asyncio
async def test_disabled_tick_logs_once_without_database(monkeypatch, caplog):
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    with caplog.at_level(logging.INFO, logger="sib"):
        assert await InvasionScheduler.tick() is None
    records = [r for r in caplog.records if r.msg == "inv2_tick"]
    assert len(records) == 1
    record = records[0]
    for name in ("spawned", "finalized", "expired", "skipped_muted",
                 "skipped_active", "skipped_no_resolver"):
        assert getattr(record, name) == 0
    assert record.duration_ms >= 0
    assert admin_runtime.last_tick_at is not None
    assert admin_runtime.last_tick_duration_ms == record.duration_ms
