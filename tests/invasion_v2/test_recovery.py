"""Startup V2 recovery: SQLite, no network, idempotent reconciliation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from sqlalchemy import func, select

from sib.database.models import Inv2Attack, Inv2GroupState, Inv2Invasion, Inv2Reward, PlayerCard
from sib.services.invasion_v2 import presentation, recovery
from sib.services.invasion_v2.recovery import InvasionV2Recovery
from sib.tests.invasion_v2.test_presentation_phase5 import db, create_inv


@pytest.mark.asyncio
async def test_recovery_expires_overdue_and_retries_victory_once(db, monkeypatch):
    factory, get_session = db
    monkeypatch.setattr(recovery, "get_session", get_session)
    monkeypatch.setattr(recovery, "is_group_available", AsyncMock(return_value=True))
    victory = await create_inv(factory, status="VICTORY", hp=1, message_id=123)
    expired = await create_inv(factory, status="ACTIVE", hp=300, message_id=124)
    async with factory.begin() as s:
        inv = await s.get(Inv2Invasion, expired)
        inv.ends_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
        s.add(Inv2Attack(invasion_id=victory, user_id=1, card_id=101,
                         card_power=20, damage=20, is_crit=False,
                         narrative="پیروزی", joined_at=datetime.now(timezone.utc)))
        s.add(Inv2Reward(invasion_id=victory, user_id=1, reward_type="golden_apple",
                         reward_amount=1, granted_at=datetime.now(timezone.utc)))
        s.add(Inv2GroupState(group_id=-100, total_victories=1, total_defeats=0))
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True),
                          send_photo=AsyncMock(return_value=True))
    first = await InvasionV2Recovery.run_on_startup(bot)
    assert first == {"expired": 1, "delivered": 1, "delivery_failed": 0,
                     "reward_mismatches": 0, "group_mismatches": 0, "disabled": False}
    again = await InvasionV2Recovery.run_on_startup(bot)
    assert again["expired"] == again["delivered"] == 0
    bot.send_photo.assert_awaited_once()
    async with factory() as s:
        assert (await s.get(Inv2Invasion, expired)).status == "EXPIRED"
        assert (await s.get(Inv2Invasion, victory)).delivered_at is not None
        assert await s.scalar(select(func.count()).select_from(Inv2Reward)) == 1


@pytest.mark.asyncio
async def test_recovery_failed_photo_and_mismatches_only_warn(db, monkeypatch, caplog):
    factory, get_session = db
    monkeypatch.setattr(recovery, "get_session", get_session)
    monkeypatch.setattr(recovery, "is_group_available", AsyncMock(return_value=True))
    victory = await create_inv(factory, status="VICTORY", hp=1, message_id=123)
    async with factory.begin() as s:
        s.add(Inv2Reward(invasion_id=victory, user_id=1, reward_type="golden_apple",
                         reward_amount=1, granted_at=datetime.now(timezone.utc)))
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True),
        send_photo=AsyncMock(side_effect=TelegramBadRequest(SendPhoto(chat_id=-100, photo="file"), "failed")))
    first = await InvasionV2Recovery.run_on_startup(bot)
    assert first["delivery_failed"] == 1
    assert first["reward_mismatches"] == first["group_mismatches"] == 1
    assert "inv2 reward without attack" in caplog.text
    assert "inv2 group totals mismatch" in caplog.text
    presentation.record_send_failure.assert_awaited_once_with(-100)
    async with factory() as s:
        assert (await s.get(Inv2Invasion, victory)).delivered_at is None
        assert await s.scalar(select(func.count()).select_from(Inv2Reward)) == 1
    bot.send_photo = AsyncMock(return_value=True)
    second = await InvasionV2Recovery.run_on_startup(bot)
    assert second["delivered"] == 1
