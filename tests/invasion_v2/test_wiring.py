"""Production entry points route to v2 only; legacy calls stay dormant."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sib.services.maintenance import service as maintenance_module
from sib.services.maintenance.service import MaintenanceService
from sib.services.invasion_v2.scheduler import InvasionScheduler
from sib.services.recovery.service import RecoveryService


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_maintenance_uses_only_v2_tick(monkeypatch, enabled):
    from sib.services.session import SessionService
    from sib.services.trading import TradeService
    from sib.services.match_result import MatchResultService
    from sib.services.apple_duel_presentation import AppleDuelPresentation
    from sib.services.player_xp import PlayerXPService
    from sib.services.invasion_telegram import InvasionTelegramService

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace(rollback=AsyncMock())

    monkeypatch.setattr(maintenance_module, "get_session", fake_session)
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=enabled))
    monkeypatch.setattr(SessionService, "reconcile_stale_state", AsyncMock())
    monkeypatch.setattr(TradeService, "expire_overdue", AsyncMock())
    monkeypatch.setattr(maintenance_module.TimeoutService, "process_acceptance_timeouts", AsyncMock())
    monkeypatch.setattr(maintenance_module.TimeoutService, "process_turn_timeouts", AsyncMock(return_value=[]))
    monkeypatch.setattr(MaintenanceService, "process_expired_queue_entries", AsyncMock())
    monkeypatch.setattr(MatchResultService, "retry_pending_xp_awards", AsyncMock(return_value=0))
    monkeypatch.setattr(MaintenanceService, "run_marriage_love_cycle", AsyncMock())
    monkeypatch.setattr(MaintenanceService, "_resume_pending_bot_turns", AsyncMock())
    monkeypatch.setattr(AppleDuelPresentation, "recover_and_publish", AsyncMock())
    monkeypatch.setattr(PlayerXPService, "deliver_pending_notifications", AsyncMock())
    old_cycle = AsyncMock()
    old_publisher = AsyncMock()
    new_tick = AsyncMock()
    monkeypatch.setattr(MaintenanceService, "run_invasion_cycle", old_cycle)
    monkeypatch.setattr(InvasionTelegramService, "publish_maintenance_updates", old_publisher)
    monkeypatch.setattr(InvasionScheduler, "tick", new_tick)

    await MaintenanceService.run_once()
    assert new_tick.await_count == int(enabled)
    old_cycle.assert_not_awaited()
    old_publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_recovery_uses_only_v2(monkeypatch, enabled):
    from sib.services.session import SessionService
    from sib.services.apple_battle import AppleBattleService
    from sib.services.marriage_proposal import MarriageProposalService
    from sib.services.love import LoveService

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(SessionService, "reconcile_stale_state", AsyncMock(return_value={}))
    monkeypatch.setattr(AppleBattleService, "recover_active_matches", AsyncMock(return_value={
        "resumed": [], "finalized": 0, "finalized_ids": [],
    }))
    monkeypatch.setattr(MarriageProposalService, "expire_overdue_proposals", AsyncMock(return_value=[]))
    monkeypatch.setattr(LoveService, "apply_daily_decay", AsyncMock(return_value=0))
    monkeypatch.setattr(LoveService, "expire_overdue_gifts", AsyncMock(return_value=[]))
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=enabled))
    old_cycle = AsyncMock()
    new_recover = AsyncMock(return_value=1)
    monkeypatch.setattr(MaintenanceService, "run_invasion_cycle", old_cycle)
    monkeypatch.setattr(InvasionScheduler, "recover", new_recover)

    await RecoveryService.recover_on_startup(session)
    assert new_recover.await_count == int(enabled)
    old_cycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_logs_resolver_once_on_startup(monkeypatch, caplog):
    from sib.services.invasion_v2.power_resolver import set_power_resolver

    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    stop = asyncio.Event()
    stop.set()
    try:
        set_power_resolver(None)
        with caplog.at_level(logging.INFO, logger="sib"):
            await MaintenanceService.run_forever(stop)
        assert caplog.text.count("inv2 power resolver: none") == 1

        class AuthoritativeResolver:
            async def average_main_card_power(self, *, group_id, session):
                return 500.0

        set_power_resolver(AuthoritativeResolver())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="sib"):
            await MaintenanceService.run_forever(stop)
        assert caplog.text.count("inv2 power resolver: AuthoritativeResolver") == 1
    finally:
        set_power_resolver(None)
