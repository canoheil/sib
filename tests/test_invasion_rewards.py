"""Golden Apple settlement for four-wave Invasions.

Covered rules:
- Rewards are real ``apple_gold`` inventory items (``user_items``), never Apple
  wallet currency.
- Only defenders (a persisted successful defense action in that Invasion) are
  paid. Joining alone never qualifies.
- Quantities: total victory 10, narrow defense 5, defeat 1 per defender.
- Settlement is exactly once: a failed grant rolls back and is retried, and a
  committed marker makes every later call a no-op.
- Interrupted (outcome NULL) and already-settled historical events are never
  paid.

Outcomes are produced by real combat (``outcome_for`` over the persisted
defense total), not injected. Row-lock races are MySQL/MariaDB-only and are
marked with ``requires_row_locks``.
"""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import AppleWallet, Invasion, InvasionOutcome, InvasionStatus
from sib.services.config_store import CONFIG_DEFAULTS
from sib.services.invasion import InvasionDefenseActionCode, InvasionService
from sib.services.invasion_rewards import (
    GOLDEN_APPLE_ITEM_KEY,
    InvasionRewardService,
    InvasionSettlementOutcomeError,
)
from sib.services.items import ItemService
from sib.services.maintenance.service import MaintenanceService
from sib.services.recovery import RecoveryService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_group, create_test_user

UTC = timezone.utc
SLOT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # a 4-hour UTC slot start


def at(seconds: float) -> datetime:
    return SLOT + timedelta(seconds=seconds)


async def _live(start: float, end: float, step: int = 15) -> Counter:
    """Tick maintenance at live cadence (no heartbeat gap looks like downtime)."""
    totals: Counter = Counter()
    current = start
    while current <= end:
        stats = await MaintenanceService.run_invasion_cycle(
            now=at(current), schedule_occurrences=False
        )
        totals.update(stats)
        current += step
    return totals


async def _battle(session) -> tuple[int, int]:
    """Create a group, activate its 12:00 four-wave battle, return (group, invasion)."""
    group = await create_test_group(session, "Golden reward group")
    group_id = int(group.id)
    await session.commit()
    await MaintenanceService.run_invasion_cycle(now=at(0))
    session.expire_all()
    invasion = await session.scalar(
        select(Invasion).where(Invasion.group_id == group_id).order_by(Invasion.id.desc())
    )
    assert invasion is not None, "expected the battle to activate in its window"
    return group_id, int(invasion.id)


async def _user(session, name: str) -> int:
    user = await create_test_user(session, name)
    user_id = int(user.id)
    await session.commit()
    return user_id


async def _join(session, group_id: int, invasion_id: int, user_id: int, seconds: float):
    await InvasionService.join(
        session, invasion_id, user_id, expected_group_id=group_id, now=at(seconds)
    )
    await session.commit()


async def _brace(
    session, group_id: int, invasion_id: int, user_id: int, wave: int, seconds: float, key: str
):
    await InvasionService.perform_defense_action(
        session,
        invasion_id,
        user_id,
        expected_group_id=group_id,
        wave_sequence=wave,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key=key,
        now=at(seconds),
    )
    await session.commit()


# The scenario ticks at live cadence up to this instant (last brace at 915 s).
# Tests then finish the battle from FINISH_FROM to its 1200 s end.
FINISH_FROM = 930


async def _finish(session, start: float = FINISH_FROM) -> Counter:
    return await _live(start, 1200)


async def _scenario(session, *, enemy_power: int):
    """Full defender (braces every wave), light defender (wave 1), join-only.

    Each brace is worth 1 point (no Main Card): 4 for the full defender plus
    1 for the light one, i.e. 5 in total. With the real threshold rule,
    ``enemy_power`` 5 -> TOTAL (5 >= 5), 6 -> NARROW (500 >= 450 and 5 < 6),
    100 -> DEFEAT.
    Maintenance ticks at live cadence so every brace lands in its own wave.
    """
    group_id, invasion_id = await _battle(session)
    invasion = await session.get(Invasion, invasion_id)
    invasion.enemy_power = enemy_power
    await session.commit()

    full = await _user(session, "Full defender")
    light = await _user(session, "Light defender")
    joiner = await _user(session, "Join only")
    await _join(session, group_id, invasion_id, full, 1)
    await _join(session, group_id, invasion_id, light, 2)
    await _join(session, group_id, invasion_id, joiner, 3)
    braces = {
        30: (light, 1, "light:1"),
        15: (full, 1, "full:1"),
        315: (full, 2, "full:2"),
        615: (full, 3, "full:3"),
        915: (full, 4, "full:4"),
    }
    for seconds in range(15, FINISH_FROM - 14, 15):
        await MaintenanceService.run_invasion_cycle(
            now=at(seconds), schedule_occurrences=False
        )
        if seconds in braces:
            user_id, wave, key = braces[seconds]
            await _brace(session, group_id, invasion_id, user_id, wave, seconds, key)
    return group_id, invasion_id, full, light, joiner


async def _golden(session, user_id: int) -> int:
    return await ItemService.get_total(session, user_id, GOLDEN_APPLE_ITEM_KEY)


async def _wallet(session, user_id: int) -> int:
    session.expire_all()
    wallet = await session.get(AppleWallet, user_id)
    return 0 if wallet is None else int(wallet.balance)


async def _invasion(session, invasion_id: int) -> Invasion:
    session.expire_all()
    return await session.get(Invasion, invasion_id)


# ----------------------------------------------------------------------
# Amounts, currency, and eligibility
# ----------------------------------------------------------------------
def test_reward_defaults_are_golden_apples_with_the_approved_quantities():
    assert CONFIG_DEFAULTS["invasion_reward_total_victory_golden_apples"] == 10
    assert CONFIG_DEFAULTS["invasion_reward_narrow_defense_golden_apples"] == 5
    assert CONFIG_DEFAULTS["invasion_reward_defeat_golden_apples"] == 1
    for legacy_key in (
        "invasion_reward_total_victory_apples",
        "invasion_reward_narrow_defense_apples",
        "invasion_reward_defeat_apples",
    ):
        assert legacy_key not in CONFIG_DEFAULTS
    assert "invasion_reward_eligibility" not in CONFIG_DEFAULTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enemy_power", "outcome", "golden"),
    [
        (5, InvasionOutcome.TOTAL_VICTORY, 10),
        (6, InvasionOutcome.NARROW_DEFENSE, 5),
        (100, InvasionOutcome.DEFEAT, 1),
    ],
)
async def test_defenders_receive_real_golden_apples_and_the_wallet_is_unchanged(
    db_session, enemy_power, outcome, golden
):
    _gid, invasion_id, full, light, joiner = await _scenario(
        db_session, enemy_power=enemy_power
    )
    # Pre-existing wallet currency must survive settlement untouched.
    db_session.add(AppleWallet(user_id=full, balance=33))
    await db_session.commit()

    totals = await _finish(db_session)  # resolution and settlement in one pass
    assert totals["invasions_resolved"] == 1
    assert totals["invasion_rewards_settled"] == 1

    invasion = await _invasion(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome == outcome
    assert invasion.rewards_settled_at is not None

    assert await _golden(db_session, full) == golden
    assert await _golden(db_session, light) == golden
    assert await _golden(db_session, joiner) == 0  # join-only: nothing
    assert await _wallet(db_session, full) == 33  # Apple wallet unchanged
    assert await _wallet(db_session, light) == 0


@pytest.mark.asyncio
async def test_join_only_battle_settles_as_a_noop_with_no_items(db_session):
    group_id, invasion_id = await _battle(db_session)
    joiner = await _user(db_session, "Only joined")
    await _join(db_session, group_id, invasion_id, joiner, 1)
    await _live(15, 1200)  # live cadence from the start: no false downtime
    invasion = await _invasion(db_session, invasion_id)
    assert invasion.outcome == InvasionOutcome.DEFEAT
    assert invasion.rewards_settled_at is not None
    assert await _golden(db_session, joiner) == 0


@pytest.mark.asyncio
async def test_unresolved_battle_pays_nothing(db_session):
    group_id, invasion_id = await _battle(db_session)
    defender = await _user(db_session, "Still fighting")
    await _join(db_session, group_id, invasion_id, defender, 1)
    await _brace(db_session, group_id, invasion_id, defender, 1, 10, "open:1")
    await _live(15, 400)
    assert await _golden(db_session, defender) == 0
    assert (await _invasion(db_session, invasion_id)).rewards_settled_at is None


# ----------------------------------------------------------------------
# Exactly once: retries, duplicates, and failed grants
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_grant_rolls_back_then_the_retry_pays_each_defender_once(
    db_session, monkeypatch
):
    _gid, invasion_id, full, light, joiner = await _scenario(db_session, enemy_power=100)
    real_grant = ItemService.grant
    state = {"fail": True, "calls": 0}

    async def flaky_grant(session, user_id, item_key, quantity=1):
        state["calls"] += 1
        # The first defender is granted successfully, then the second write fails.
        if state["fail"] and state["calls"] == 2:
            raise RuntimeError("inventory write failed")
        return await real_grant(session, user_id, item_key, quantity)

    monkeypatch.setattr(ItemService, "grant", staticmethod(flaky_grant))
    first = await _finish(db_session)
    assert first["invasion_failures"] == 1
    assert first["invasion_rewards_settled"] == 0

    # The whole settlement rolled back: the first grant did not survive either.
    invasion = await _invasion(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.rewards_settled_at is None  # marker never ran ahead of items
    assert await _golden(db_session, full) == 0
    assert await _golden(db_session, light) == 0

    state["fail"] = False
    retry = await _live(1215, 1230)
    assert retry["invasion_rewards_settled"] == 1
    assert await _golden(db_session, full) == 1
    assert await _golden(db_session, light) == 1
    assert await _golden(db_session, joiner) == 0

    later = await _live(1245, 1300)
    assert later["invasion_rewards_settled"] == 0
    assert await _golden(db_session, full) == 1
    assert await _golden(db_session, light) == 1


@pytest.mark.asyncio
async def test_repeated_settlement_and_restart_never_pay_twice(db_session):
    _gid, invasion_id, full, light, _joiner = await _scenario(db_session, enemy_power=100)
    await _finish(db_session)
    await RecoveryService.recover_on_startup(db_session, now=at(1300))

    session_maker = get_async_sessionmaker()
    async with session_maker() as session:
        duplicate = await InvasionRewardService.settle_completed_invasion(session, invasion_id)
        await session.commit()
    assert duplicate.duplicate is True
    assert duplicate.newly_granted_golden_apples == 0

    assert await _golden(db_session, full) == 1
    assert await _golden(db_session, light) == 1


@pytest.mark.asyncio
async def test_settlement_does_not_commit_and_caller_can_roll_back(db_session):
    _gid, invasion_id, full, _light, _joiner = await _scenario(db_session, enemy_power=100)
    # Finish without settlement by advancing the timeline directly.
    for seconds in range(FINISH_FROM, 1201, 15):
        await InvasionService.advance_battle(db_session, invasion_id, now=at(seconds))
        await db_session.commit()
    invasion = await _invasion(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.rewards_settled_at is None

    await InvasionRewardService.settle_completed_invasion(db_session, invasion_id, now=at(1200))
    await db_session.rollback()
    assert await _golden(db_session, full) == 0
    assert (await _invasion(db_session, invasion_id)).rewards_settled_at is None


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_settlement_attempts_grant_each_defender_once(db_session):
    _gid, invasion_id, full, light, _joiner = await _scenario(db_session, enemy_power=100)
    for seconds in range(FINISH_FROM, 1201, 15):
        await InvasionService.advance_battle(db_session, invasion_id, now=at(seconds))
        await db_session.commit()
    session_maker = get_async_sessionmaker()

    async def settle_once():
        async with session_maker() as session:
            result = await InvasionRewardService.settle_completed_invasion(session, invasion_id)
            await session.commit()
            return result

    first, second = await asyncio.gather(settle_once(), settle_once())
    assert sorted([first.duplicate, second.duplicate]) == [False, True]
    assert await _golden(db_session, full) == 1
    assert await _golden(db_session, light) == 1


# ----------------------------------------------------------------------
# History and interruption
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_already_settled_history_is_never_paid_retroactively(db_session):
    _gid, invasion_id, full, light, _joiner = await _scenario(db_session, enemy_power=100)
    for seconds in range(FINISH_FROM, 1201, 15):
        await InvasionService.advance_battle(db_session, invasion_id, now=at(seconds))
        await db_session.commit()
    # Simulates the migration backfill: a FINISHED event already marked settled.
    invasion = await _invasion(db_session, invasion_id)
    invasion.rewards_settled_at = at(1200)
    await db_session.commit()

    totals = await _live(1215, 1400)
    assert totals["invasion_rewards_settled"] == 0
    assert await _golden(db_session, full) == 0
    assert await _golden(db_session, light) == 0


@pytest.mark.asyncio
async def test_interrupted_battle_is_never_paid_and_is_not_settleable(db_session):
    group_id, invasion_id = await _battle(db_session)
    defender = await _user(db_session, "Defender before the stall")
    await _join(db_session, group_id, invasion_id, defender, 1)
    await _brace(db_session, group_id, invasion_id, defender, 1, 10, "stall:1")
    await _live(15, 60)
    stats = await RecoveryService.recover_on_startup(db_session, now=at(1400))
    assert stats["invasions_interrupted"] == 1

    invasion = await _invasion(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome is None

    later = await _live(1415, 1800)
    assert later["invasion_rewards_settled"] == 0
    assert await _golden(db_session, defender) == 0
    with pytest.raises(InvasionSettlementOutcomeError):
        await InvasionRewardService.settle_completed_invasion(db_session, invasion_id)
