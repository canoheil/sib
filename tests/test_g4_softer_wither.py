"""G4 — soften garden withering with offline grace and max 2 limit."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from sib.database.models import GardenPlot, GardenWitherEvent, Inv2GroupMember, Inv2Region, Inv2Invasion
from sib.services.garden import GardenService
from sib.services.invasion_v2.enums import InvasionStatus
from sib.services.invasion_v2.rewards_service import settle_outcome
from sib.tests.test_parties import create_test_group, create_test_user

REGION_CODE = "g4-moon"
NOW = lambda: datetime.now(timezone.utc)


async def _make_region(session):
    region = Inv2Region(code=REGION_CODE, name_fa="درّه ماه", controlling_faction="فرزندان ماه")
    session.add(region)
    await session.flush()
    return region


async def _make_member(session, user_id: int, group_id: int, *, last_seen_delta: timedelta | None = None):
    # last_seen_delta is how long ago; None means now (active)
    if last_seen_delta is None:
        last_seen = NOW()
    else:
        last_seen = NOW() - last_seen_delta
    session.add(Inv2GroupMember(user_id=user_id, group_id=group_id, last_seen_at=last_seen))
    await session.flush()


async def _make_invasion(session, group_id: int, *, status: str, announced: bool = True):
    now = NOW()
    inv = Inv2Invasion(
        group_id=group_id,
        region_code=REGION_CODE,
        enemy_type="soldier",
        enemy_name_fa="شیاسیب پیاده",
        enemy_hp=100,
        enemy_hp_remaining=100,
        difficulty_multiplier=1.0,
        status=status,
        scheduled_at=now,
        started_at=now if announced else None,
        ends_at=now if announced else None,
        finalized_at=now,
        message_id=555 if announced else None,
        message_chat_id=group_id if announced else None,
        occurrence_key=f"g4:{uuid.uuid4().hex}",
    )
    session.add(inv)
    await session.flush()
    return inv


async def _withered_count(session, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GardenPlot)
            .where(GardenPlot.garden_id == user_id, GardenPlot.is_withered.is_(True))
        )
    )


@pytest.mark.asyncio
async def test_offline_users_are_exempt_from_withering(db_session):
    """Offline >4h must be exempt; only recently active within 4h can be withered."""
    await _make_region(db_session)
    group = await create_test_group(db_session, "گروه گ۴ آفلاین")

    offline_user = await create_test_user(db_session, "خواب‌آلود قدیمی")
    active_user = await create_test_user(db_session, "بیدار فعال")

    await GardenService.get_or_create(db_session, offline_user.id)
    await GardenService.get_or_create(db_session, active_user.id)

    # offline: seen 5 hours ago (>4h grace)
    await _make_member(db_session, offline_user.id, group.id, last_seen_delta=timedelta(hours=5))
    # active: seen now (within 4h)
    await _make_member(db_session, active_user.id, group.id, last_seen_delta=None)

    invasion = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)

    results = await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    # offline must NOT be withered
    assert await _withered_count(db_session, offline_user.id) == 0
    assert offline_user.id not in [r.user_id for r in results]
    # active with zero damage CAN be withered (positive control)
    assert await _withered_count(db_session, active_user.id) == 1
    assert active_user.id in [r.user_id for r in results]

    # All offline scenario: no one withered, no group notification
    group2 = await create_test_group(db_session, "گروه همه آفلاین")
    offline_only = await create_test_user(db_session, "آفلاین تنها")
    await GardenService.get_or_create(db_session, offline_only.id)
    await _make_member(db_session, offline_only.id, group2.id, last_seen_delta=timedelta(hours=20))
    inv2 = await _make_invasion(db_session, group2.id, status=InvasionStatus.DEFEAT.value, announced=True)
    results2 = await settle_outcome(db_session, inv2.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert results2 == []
    assert await _withered_count(db_session, offline_only.id) == 0

    # Edge: exactly 4h ago should still be considered? Spec says "within last 4 hours" => >= 4h
    # We test 3h ago is still eligible (active)
    edge_user = await create_test_user(db_session, "لب‌مرز")
    await GardenService.get_or_create(db_session, edge_user.id)
    # Use a new group for edge test to avoid daily cap with previous invasion
    group_edge = await create_test_group(db_session, "گروه لب‌مرز")
    await _make_member(db_session, edge_user.id, group_edge.id, last_seen_delta=timedelta(hours=3))
    inv_edge = await _make_invasion(db_session, group_edge.id, status=InvasionStatus.DEFEAT.value, announced=True)
    results_edge = await settle_outcome(db_session, inv_edge.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert edge_user.id in [r.user_id for r in results_edge]


@pytest.mark.asyncio
async def test_withering_is_limited_to_two_users_max(db_session):
    """Even with many eligible users, at most 2 gardens withered per invasion."""
    await _make_region(db_session)
    group = await create_test_group(db_session, "گروه شلوغ گ۴")

    users = []
    for i in range(5):
        u = await create_test_user(db_session, f"مدافع{i}")
        await GardenService.get_or_create(db_session, u.id)
        await _make_member(db_session, u.id, group.id, last_seen_delta=None)
        users.append(u)

    invasion = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)

    results = await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    # Must be max 2, and with 5 eligible should be exactly 2 (shuffled selection)
    assert len(results) <= 2
    assert len(results) == 2, "with 5 eligible active users, exactly 2 should be withered (max 2 limit)"
    assert len(set(r.user_id for r in results)) == len(results)  # no duplicates

    # Verify each selected user actually has a withered plot
    for r in results:
        assert await _withered_count(db_session, r.user_id) == 1

    # Other users (not selected) should remain healthy (due to limit, not all withered)
    not_selected = [u for u in users if u.id not in [r.user_id for r in results]]
    assert len(not_selected) == 3
    for u in not_selected:
        assert await _withered_count(db_session, u.id) == 0

    # With 1 eligible, should be 1 (not 2)
    group_single = await create_test_group(db_session, "گروه تک‌نفره")
    solo = await create_test_user(db_session, "تنها مدافع")
    await GardenService.get_or_create(db_session, solo.id)
    await _make_member(db_session, solo.id, group_single.id, last_seen_delta=None)
    inv_single = await _make_invasion(db_session, group_single.id, status=InvasionStatus.DEFEAT.value, announced=True)
    res_single = await settle_outcome(db_session, inv_single.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert len(res_single) == 1
    assert res_single[0].user_id == solo.id

    # With 0 eligible (all offline), must be 0
    group_empty = await create_test_group(db_session, "گروه خالی")
    for i in range(3):
        u = await create_test_user(db_session, f"آفلاین{i}")
        await GardenService.get_or_create(db_session, u.id)
        await _make_member(db_session, u.id, group_empty.id, last_seen_delta=timedelta(hours=15))
    inv_empty = await _make_invasion(db_session, group_empty.id, status=InvasionStatus.DEFEAT.value, announced=True)
    res_empty = await settle_outcome(db_session, inv_empty.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert res_empty == []


@pytest.mark.asyncio
async def test_active_users_with_zero_damage_can_be_withered(db_session):
    """Active within 4h with zero damage must still be witherable."""
    await _make_region(db_session)
    group = await create_test_group(db_session, "گروه فعال گ۴")

    active = await create_test_user(db_session, "فعال صفر")
    await GardenService.get_or_create(db_session, active.id)
    await _make_member(db_session, active.id, group.id, last_seen_delta=timedelta(hours=1))

    invasion = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)

    results = await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    assert len(results) == 1
    assert results[0].user_id == active.id
    assert await _withered_count(db_session, active.id) == 1

    # Ensure withered count is exactly one per user per invasion (daily cap)
    # Second invasion same day should not wither same user again
    inv2 = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)
    results2 = await settle_outcome(db_session, inv2.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert results2 == []
    assert await _withered_count(db_session, active.id) == 1  # still one

    # Participant (has attack) must remain exempt even if active
    from sib.database.models import Character, Inv2Attack, PlayerCard

    attacker = await create_test_user(db_session, "مدافع مهاجم")
    await GardenService.get_or_create(db_session, attacker.id)
    await _make_member(db_session, attacker.id, group.id, last_seen_delta=timedelta(hours=1))
    # create attack row for attacker
    char = Character(code=f"g4-char-{uuid.uuid4().hex[:6]}", name_fa="کاراکتر تست", power=10, speed=10, intelligence=10)
    db_session.add(char)
    await db_session.flush()
    card = PlayerCard(user_id=attacker.id, character_id=char.id, level=1, is_favorite=True)
    db_session.add(card)
    await db_session.flush()
    inv3 = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)
    db_session.add(
        Inv2Attack(
            invasion_id=inv3.id,
            user_id=attacker.id,
            card_id=card.id,
            card_power=10,
            damage=0,
            is_crit=False,
            narrative="ضربه تست",
            joined_at=NOW(),
        )
    )
    await db_session.flush()
    # Need a fresh active zero-damage victim to ensure invasion still withers someone else
    victim = await create_test_user(db_session, "قربانی فعال")
    await GardenService.get_or_create(db_session, victim.id)
    await _make_member(db_session, victim.id, group.id, last_seen_delta=timedelta(hours=2))
    results3 = await settle_outcome(db_session, inv3.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert attacker.id not in [r.user_id for r in results3]
    assert victim.id in [r.user_id for r in results3]


@pytest.mark.asyncio
async def test_withering_grace_period_reads_from_config(db_session):
    """Grace period must be read from system_config and be tunable (G4.5)."""
    await _make_region(db_session)
    from sib.services.config_store import ConfigService

    # Default is 4h: user seen 5h ago should be exempt
    group_default = await create_test_group(db_session, "گروه پیش‌فرض ۴ساعته")
    user_5h = await create_test_user(db_session, "۵ساعته پیش‌فرض")
    await GardenService.get_or_create(db_session, user_5h.id)
    await _make_member(db_session, user_5h.id, group_default.id, last_seen_delta=timedelta(hours=5))
    inv_default = await _make_invasion(
        db_session, group_default.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    results_default = await settle_outcome(db_session, inv_default.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert user_5h.id not in [r.user_id for r in results_default]
    assert await _withered_count(db_session, user_5h.id) == 0

    # Change config to 8h: same 5h user should now be within grace and be withered
    await ConfigService.set(db_session, "garden_wither_grace_hours", 8)
    await db_session.commit()

    group_8h = await create_test_group(db_session, "گروه ۸ساعته")
    user_5h_8 = await create_test_user(db_session, "۵ساعته ۸ساعته")
    await GardenService.get_or_create(db_session, user_5h_8.id)
    await _make_member(db_session, user_5h_8.id, group_8h.id, last_seen_delta=timedelta(hours=5))
    inv_8h = await _make_invasion(
        db_session, group_8h.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    results_8h = await settle_outcome(db_session, inv_8h.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert user_5h_8.id in [r.user_id for r in results_8h]
    assert await _withered_count(db_session, user_5h_8.id) == 1

    # Invalid config should fallback to 4h
    await ConfigService.set(db_session, "garden_wither_grace_hours", "invalid")
    await db_session.commit()

    group_invalid = await create_test_group(db_session, "گروه نامعتبر")
    user_5h_invalid = await create_test_user(db_session, "۵ساعته نامعتبر")
    await GardenService.get_or_create(db_session, user_5h_invalid.id)
    await _make_member(
        db_session, user_5h_invalid.id, group_invalid.id, last_seen_delta=timedelta(hours=5)
    )
    inv_invalid = await _make_invasion(
        db_session, group_invalid.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    results_invalid = await settle_outcome(db_session, inv_invalid.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    # Should fallback to 4h, so 5h user exempt
    assert user_5h_invalid.id not in [r.user_id for r in results_invalid]
    assert await _withered_count(db_session, user_5h_invalid.id) == 0
