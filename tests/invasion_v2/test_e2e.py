"""Full V2 tick → announcement → joins → settlement → retry/recovery, offline."""

import asyncio
from datetime import datetime, timezone
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from sqlalchemy import func, select

from sib.database.models import (Inv2Attack, Inv2GroupState, Inv2Invasion,
                                 Inv2Region, Inv2Reward, PlayerCard, User)
from sib.handlers import inv2_join
from sib.services.config_store import ConfigService
from sib.services.invasion_v2 import circuit_breaker, presentation, recovery, scheduler
from sib.services.invasion_v2.enums import EnemyType
from sib.services.invasion_v2.narrative_pool import JOIN_KILL
from sib.services.invasion_v2.power_resolver import set_power_resolver
from sib.services.invasion_v2.recovery import InvasionV2Recovery
from sib.services.invasion_v2.scheduler import InvasionScheduler
from sib.tests.invasion_v2.test_presentation_phase5 import db, callback


@pytest.mark.asyncio
async def test_v2_full_battle_offline(db, monkeypatch):
    factory, get_session = db  # isolated in-memory SQLite, all Telegram mocked
    monkeypatch.setattr(scheduler, "get_session", get_session)
    monkeypatch.setattr(circuit_breaker, "get_session", get_session)
    monkeypatch.setattr(recovery, "get_session", get_session)
    monkeypatch.setattr(presentation, "record_send_failure", circuit_breaker.record_send_failure)
    monkeypatch.setattr(presentation, "record_send_success", circuit_breaker.record_send_success)
    monkeypatch.setattr(scheduler, "choose_enemy_type", lambda rng: EnemyType.SOLDIER)
    monkeypatch.setattr(inv2_join, "_answer", AsyncMock())  # no callback HTTP
    # Await detached delivery deterministically instead of assuming 30ms is enough.
    joined = {uid: asyncio.Event() for uid in range(1, 17)}
    finalized = asyncio.Event()
    original_join = presentation.InvasionPresentation.on_join
    original_finalize = presentation.InvasionPresentation.finalize

    async def tracked_join(self, bot, invasion_id, user_id, attack_id):
        try:
            return await original_join(self, bot, invasion_id, user_id, attack_id)
        finally:
            joined[user_id].set()

    async def tracked_finalize(self, bot, invasion_id):
        try:
            return await original_finalize(self, bot, invasion_id)
        finally:
            finalized.set()

    monkeypatch.setattr(presentation.InvasionPresentation, "on_join", tracked_join)
    monkeypatch.setattr(presentation.InvasionPresentation, "finalize", tracked_finalize)
    set_power_resolver(SimpleNamespace(average_main_card_power=AsyncMock(return_value=500.0)))
    try:
        async with factory.begin() as s:
            await ConfigService.set(s, "invasion_enabled", True)  # TEST DB only
            await ConfigService.set(s, "invasion_difficulty_multiplier", 0.3)
            for uid in range(3, 17):
                s.add(User(id=uid, first_name=f"مدافع {uid}"))
            await s.flush()
            for uid in range(1, 17):
                s.add(PlayerCard(id=100 + uid, user_id=uid, character_id=10,
                                 level=1, is_favorite=True))  # Power 20 each
        bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-100), message_id=123)),
            edit_message_caption=AsyncMock(return_value=True))
        # Fixed Tehran-local 20:00 spawn slot (see InvasionScheduler._current_slot)
        # rather than the real wall clock, so this test is deterministic.
        now = datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc)
        await InvasionScheduler.tick(now=now, rng=Random(4))
        async with factory() as s:
            inv = await s.scalar(select(Inv2Invasion))
            assert inv is not None and inv.status == "PENDING"
            inv_id = inv.id
        # The scheduler is intentionally DB-only. Maintenance invokes this
        # presentation step after tick commits, not inside tick itself.
        await presentation.InvasionPresentation().publish_pending(bot)
        assert bot.send_photo.await_count == 1
        assert bot.send_photo.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"inv2:join:{inv_id}"
        async with factory() as s:
            inv = await s.get(Inv2Invasion, inv_id)
            assert inv.status == "ACTIVE" and inv.enemy_hp == 150
        for uid in range(1, 4):
            async with factory() as s:
                old_hp = (await s.get(Inv2Invasion, inv_id)).enemy_hp_remaining
            await inv2_join.handle_inv2_join(callback(inv_id, user_id=uid), bot)
            await asyncio.wait_for(joined[uid].wait(), timeout=5)
            async with factory() as s:
                inv = await s.get(Inv2Invasion, inv_id)
                attack = await s.scalar(select(Inv2Attack).where(
                    Inv2Attack.invasion_id == inv_id, Inv2Attack.user_id == uid))
                assert attack.card_power == 20
                assert inv.enemy_hp_remaining == max(0, old_hp - attack.damage)
                assert await s.scalar(select(func.count()).select_from(Inv2Attack)) == uid
        for uid in range(4, 17):
            async with factory() as s:
                if (await s.get(Inv2Invasion, inv_id)).status == "VICTORY":
                    break
            await inv2_join.handle_inv2_join(callback(inv_id, user_id=uid), bot)
            await asyncio.wait_for(joined[uid].wait(), timeout=5)
        await asyncio.wait_for(finalized.wait(), timeout=5)
        async with factory() as s:
            inv = await s.get(Inv2Invasion, inv_id)
            assert inv.status == "VICTORY" and inv.enemy_hp_remaining == 0
            assert (await s.get(Inv2Region, inv.region_code)).is_lost_to_enemy is False
            attacks = (await s.scalars(select(Inv2Attack).where(Inv2Attack.invasion_id == inv_id))).all()
            rewards = (await s.scalars(select(Inv2Reward).where(Inv2Reward.invasion_id == inv_id))).all()
            assert len(attacks) >= 4
            finisher = max(attacks, key=lambda attack: attack.id)
            name = (await s.get(User, finisher.user_id)).display_name
            assert finisher.narrative in {text.format(user=name, card="سیب سبز") for text in JOIN_KILL}
            assert {r.user_id for r in rewards} == {a.user_id for a in attacks}
            assert len(rewards) >= len(attacks)
            before = len(rewards)
            assert inv.delivered_at is not None
        assert bot.send_photo.await_count >= 2  # announcement and victory photo
        await InvasionScheduler.tick(now=now, rng=Random(4))
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(Inv2Invasion)) == 1
        failure = AsyncMock(side_effect=TelegramBadRequest(
            SendPhoto(chat_id=-100, photo="file"), "rejected"))
        assert await presentation._telegram(-100, failure, photo="file") is None
        async with factory() as s:
            assert (await s.get(Inv2GroupState, -100)).consecutive_failures == 1
        recovered = await InvasionV2Recovery.run_on_startup(bot)
        assert recovered["expired"] == recovered["delivered"] == 0
        assert recovered["reward_mismatches"] == recovered["group_mismatches"] == 0
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(Inv2Reward)) == before
            assert (await s.get(Inv2Invasion, inv_id)).status == "VICTORY"
    finally:
        set_power_resolver(None)
