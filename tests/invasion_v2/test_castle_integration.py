"""Exercise the real join handler: castle rounding occurs before item doubling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from sqlalchemy import select
from sib.database.models import GroupCastle, Inv2Attack, Inv2Invasion
from sib.handlers import inv2_join
from sib.services.invasion_v2.presentation import InvasionPresentation
from sib.services.items import ItemService
from sib.tests.invasion_v2.test_presentation_phase5 import db, callback, create_inv
from sib.database.models import PlayerCard


@pytest.mark.parametrize("use_item, expected", [(False, 151), (True, 302)])
async def test_actual_join_castle_before_sib_buff(db, monkeypatch, use_item, expected):
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", hp=1000, message_id=123)
    async with factory.begin() as s:
        s.add(GroupCastle(group_id=-100, level=10, total_donated=1300000))
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
        await ItemService.grant(s, 1, "sib_buff", 1)
    monkeypatch.setattr(inv2_join, "compute_damage", Mock(return_value=(101, False)))
    monkeypatch.setattr(inv2_join, "_answer", AsyncMock())
    delivered = asyncio.Event()
    monkeypatch.setattr(
        InvasionPresentation,
        "on_join",
        AsyncMock(side_effect=lambda *args: delivered.set()),
    )
    cb = callback(inv_id)
    if use_item:
        cb = cb.model_copy(update={"data": f"inv2:joinbuff:{inv_id}"})
    await inv2_join.handle_inv2_join(cb, SimpleNamespace())
    await asyncio.wait_for(delivered.wait(), timeout=5)
    async with factory() as s:
        attack = await s.scalar(select(Inv2Attack))
        assert attack.damage == expected
        assert (await s.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 1000 - expected
        assert await ItemService.get_total(s, 1, "sib_buff") == (0 if use_item else 1)
