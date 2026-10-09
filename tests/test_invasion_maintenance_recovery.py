"""Scheduler policy, legacy compatibility, and restart recovery for Invasions.

Four-wave activation, timeline, outage, and settlement behaviour live in
``test_invasion_four_wave.py``. This module covers the shared scheduler policy
(activation only inside a slot window, no pre-creation, no backfill, no
overlap) and proves that pre-existing two-wave events keep their timeline.

Row-lock races are MySQL/MariaDB-only and are marked with ``requires_row_locks``.
"""

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from sib.database import Group, GroupStats
from sib.database.models import (
    Invasion,
    InvasionOutcome,
    InvasionStatus,
    InvasionWave,
)
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.invasion import InvasionService
from sib.services.maintenance.service import MaintenanceService
from sib.services.recovery import RecoveryService
from sib.tests.conftest import requires_row_locks

UTC = timezone.utc
SLOT = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)  # a 4-hour UTC slot start
BASE_TIME = SLOT


async def _run_ticks(start: datetime, end: datetime, step_seconds: int = 15) -> None:
    """Run maintenance at a live cadence, so no heartbeat gap looks like downtime."""
    current = start
    while current <= end:
        await MaintenanceService.run_invasion_cycle(now=current, schedule_occurrences=False)
        current += timedelta(seconds=step_seconds)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _make_group(session, *, matches_played: int = 0, is_active: bool = True):
    """Persist a Group and return a plain id holder.

    Returning the ORM object would let later ``expire_all()`` calls expire it,
    and reading an expired attribute in async code triggers a sync lazy load.
    """
    group_id = -(10_000 + int(uuid.uuid4().int % 1_000_000_000))
    session.add(Group(id=group_id, title=f"Invasion schedule test {group_id}", is_active=is_active))
    session.add(
        GroupStats(
            group_id=group_id,
            matches_played=matches_played,
            wins=0,
            losses=0,
            draws=0,
        )
    )
    await session.commit()
    return SimpleNamespace(id=group_id)


def _legacy_event(scheduled_at: datetime, matches_played: int = 0) -> tuple:
    """The exact Phase-5 two-wave snapshot shape, built without the scheduler."""
    slot_epoch = int(scheduled_at.timestamp())
    enemy_power = 100 + min(max(0, matches_played), 100)
    deadlines = [
        (scheduled_at + timedelta(seconds=600 * step))
        .astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
        for step in (1, 2)
    ]
    event_config = {
        "schema_version": 1,
        "occurrence_slot_epoch": slot_epoch,
        "wave_count": 2,
        "wave_durations_seconds": [600, 600],
        "wave_deadlines_utc": deadlines,
        "planned_end_at_utc": deadlines[-1],
        "enemy_power": enemy_power,
    }
    compositions = [
        {"wave_sequence": 1, "enemy_power": enemy_power // 2 + enemy_power % 2},
        {"wave_sequence": 2, "enemy_power": enemy_power // 2},
    ]
    return enemy_power, event_config, compositions


async def _make_legacy_invasion(session, group: Group, *, scheduled_at: datetime) -> Invasion:
    enemy_power, event_config, compositions = _legacy_event(scheduled_at)
    invasion = await InvasionService.create(
        session,
        group_id=group.id,
        occurrence_key=f"legacy-test:{uuid.uuid4().hex}",
        scheduled_at=scheduled_at,
        enemy_faction_code=MaintenanceService.INVASION_ENEMY_FACTION,
        enemy_power=enemy_power,
        event_config=event_config,
        wave_compositions=compositions,
    )
    await session.commit()
    return invasion


async def _reload(session, invasion_id: int) -> Invasion:
    session.expire_all()
    invasion = await session.get(Invasion, invasion_id)
    assert invasion is not None
    return invasion


async def _group_invasions(session, group_id: int) -> list[Invasion]:
    session.expire_all()
    return list(
        (
            await session.scalars(
                select(Invasion)
                .where(Invasion.group_id == group_id)
                .order_by(Invasion.scheduled_at.asc())
            )
        ).all()
    )


# ----------------------------------------------------------------------
# Scheduler policy (shared by every battle)
# ----------------------------------------------------------------------
def test_slot_window_is_half_open_and_never_pre_creates():
    window = 20 * 60
    interval = 4 * 60 * 60
    eligible = MaintenanceService._eligible_slot
    slot = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)

    assert eligible(slot, interval, window)[1] == slot  # exact start: eligible
    assert eligible(slot + timedelta(seconds=window - 1), interval, window)[1] == slot
    assert eligible(slot + timedelta(seconds=window), interval, window) is None  # closed
    assert eligible(slot - timedelta(seconds=1), interval, window) is None  # previous slot
    assert eligible(slot + timedelta(hours=3, minutes=40), interval, window) is None


def test_default_cadence_is_four_hours_with_six_utc_aligned_slots():
    assert int(CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"]) == 4 * 60 * 60
    day = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    starts = []
    for slot_index in range(6):
        now = day + timedelta(hours=4 * slot_index, minutes=5)
        slot_epoch, scheduled_at = MaintenanceService._eligible_slot(
            now, 4 * 60 * 60, 4 * 300
        )
        assert scheduled_at == day + timedelta(hours=4 * slot_index)
        starts.append(slot_epoch)
    assert len(set(starts)) == 6
    keys = {MaintenanceService._occurrence_key(-100700, epoch) for epoch in starts}
    assert len(keys) == 6


@pytest.mark.asyncio
async def test_default_battle_config_is_four_waves_of_five_minutes(db_session):
    cfg = await MaintenanceService._battle_config(db_session)
    assert cfg.interval_seconds == 4 * 60 * 60
    assert cfg.wave_count == 4
    assert cfg.wave_seconds == 300
    assert cfg.wave_count * cfg.wave_seconds == 20 * 60
    assert cfg.max_compensation_seconds == 20 * 60


@pytest.mark.asyncio
async def test_downtime_budget_is_configurable_and_falls_back_when_invalid(db_session):
    await ConfigService.set(db_session, "invasion_downtime_compensation_max_seconds", 0)
    assert (await MaintenanceService._battle_config(db_session)).max_compensation_seconds == 0
    await ConfigService.set(db_session, "invasion_downtime_compensation_max_seconds", -5)
    assert (await MaintenanceService._battle_config(db_session)).max_compensation_seconds == 1200
    await ConfigService.set(db_session, "invasion_downtime_compensation_max_seconds", "soon")
    assert (await MaintenanceService._battle_config(db_session)).max_compensation_seconds == 1200
    await db_session.commit()


@pytest.mark.asyncio
async def test_invalid_battle_config_falls_back_to_defaults(db_session):
    await ConfigService.set(db_session, "invasion_wave_count", 0)
    await ConfigService.set(db_session, "invasion_wave_duration_seconds", 2000)
    cfg = await MaintenanceService._battle_config(db_session)
    assert cfg.wave_count == 4
    assert cfg.wave_seconds == 300
    await db_session.commit()


@pytest.mark.asyncio
async def test_scheduler_creates_one_occurrence_per_slot_per_day_and_never_early(db_session):
    group = await _make_group(db_session)
    day = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    for slot_index in range(6):
        slot = day + timedelta(hours=4 * slot_index)
        # Before this slot's window there is nothing for it: no early row exists.
        await MaintenanceService.run_invasion_cycle(now=slot - timedelta(seconds=1))
        assert len(await _group_invasions(db_session, group.id)) == slot_index
        stats = await MaintenanceService.run_invasion_cycle(now=slot + timedelta(minutes=5))
        assert stats["invasion_failures"] == 0
        # The live loop keeps ticking through the battle; the next slot is never
        # pre-created while this one is still unfinished or after it ends.
        await _run_ticks(slot + timedelta(minutes=5), slot + timedelta(minutes=40))
        assert len(await _group_invasions(db_session, group.id)) == slot_index + 1

    invasions = await _group_invasions(db_session, group.id)
    assert len(invasions) == 6
    assert [_utc(item.scheduled_at) for item in invasions] == [
        day + timedelta(hours=4 * index) for index in range(6)
    ]
    for invasion in invasions:
        # Activated inside its own window, never announced as SCHEDULED.
        assert invasion.status != InvasionStatus.SCHEDULED
        assert _utc(invasion.started_at) - _utc(invasion.scheduled_at) == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_no_battle_is_created_after_the_window_closes(db_session):
    group = await _make_group(db_session)
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(minutes=20))
    assert await _group_invasions(db_session, group.id) == []


@pytest.mark.asyncio
async def test_missed_slots_are_skipped_not_backfilled_after_an_outage(db_session):
    group = await _make_group(db_session)
    # The bot was down through the 08:00 window and wakes at 08:25: nothing is
    # created for 08:00, and no future slot is pre-created.
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(minutes=25))
    assert await _group_invasions(db_session, group.id) == []
    # The 12:00 window is the next opportunity; only that battle is created.
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(hours=4, minutes=2))
    invasions = await _group_invasions(db_session, group.id)
    assert [_utc(item.scheduled_at) for item in invasions] == [SLOT + timedelta(hours=4)]


@pytest.mark.asyncio
async def test_repeated_and_restarted_cycles_do_not_duplicate_the_occurrence(db_session):
    group = await _make_group(db_session, matches_played=12)
    now = SLOT + timedelta(minutes=1)
    first = await MaintenanceService.run_invasion_cycle(now=now)
    second = await MaintenanceService.run_invasion_cycle(now=now)
    restart = await RecoveryService.recover_on_startup(db_session, now=now)
    assert first["invasions_scheduled"] >= 1
    assert second["invasions_scheduled"] == 0
    assert restart["invasions_scheduled"] == 0
    invasions = await _group_invasions(db_session, group.id)
    assert len(invasions) == 1
    assert invasions[0].occurrence_key == MaintenanceService._occurrence_key(
        group.id, int(SLOT.timestamp())
    )


@pytest.mark.asyncio
async def test_unfinished_battle_blocks_a_new_occurrence_for_the_same_group(db_session):
    group = await _make_group(db_session)
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(minutes=1))
    blocking = await _group_invasions(db_session, group.id)
    assert len(blocking) == 1 and blocking[0].status == InvasionStatus.ACTIVE
    blocking_id = blocking[0].id
    # The 08:00 battle is still ACTIVE when the 12:00 window opens (the bot was
    # down since 08:01). The overlap guard must not start a second battle; the
    # stall is then over budget, so the 08:00 battle is interrupted instead.
    stats = await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(hours=4, minutes=1))
    assert stats["invasions_skipped_overlap"] == 1
    assert stats["invasions_interrupted"] == 1
    invasions = await _group_invasions(db_session, group.id)
    assert [item.id for item in invasions] == [blocking_id]
    assert invasions[0].status == InvasionStatus.FINISHED
    assert invasions[0].outcome is None

    # The Group is released at once: the still-open 12:00 window opens its own
    # battle on the next tick. The missed 08:00 slot itself is not replayed.
    released = await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(hours=4, minutes=1, seconds=15)
    )
    assert released["invasions_scheduled"] == 1
    invasions = await _group_invasions(db_session, group.id)
    assert [_utc(item.scheduled_at) for item in invasions] == [
        SLOT,
        SLOT + timedelta(hours=4),
    ]
    assert invasions[1].status == InvasionStatus.ACTIVE


@pytest.mark.asyncio
async def test_interrupted_battle_never_backfills_the_slots_it_outlived(db_session):
    group = await _make_group(db_session)
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(minutes=1))
    # Down from 08:01 until 20:05: four occurrences elapsed (12:00 and 16:00
    # included) while the 08:00 battle was stuck. None of them is backfilled.
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(hours=12, minutes=5))
    await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(hours=12, minutes=5, seconds=15)
    )
    invasions = await _group_invasions(db_session, group.id)
    assert [_utc(item.scheduled_at) for item in invasions] == [
        SLOT,
        SLOT + timedelta(hours=12),
    ]
    assert invasions[0].status == InvasionStatus.FINISHED
    assert invasions[0].outcome is None
    assert invasions[1].status == InvasionStatus.ACTIVE


# ----------------------------------------------------------------------
# Legacy (two-wave) events keep their original timeline
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_legacy_scheduled_inside_first_window_starts_on_recovery(db_session):
    group = await _make_group(db_session)
    invasion = await _make_legacy_invasion(db_session, group, scheduled_at=SLOT)
    await RecoveryService.recover_on_startup(db_session, now=SLOT + timedelta(minutes=2))
    persisted = await _reload(db_session, invasion.id)
    assert persisted.status == InvasionStatus.ACTIVE
    wave = await InvasionService.get_current_wave(db_session, invasion.id)
    assert _utc(wave.deadline_at) == SLOT + timedelta(minutes=10)
    # Legacy events never receive the four-wave heartbeat or stage.
    assert persisted.heartbeat_at is None
    assert persisted.presentation_stage is None


@pytest.mark.asyncio
async def test_legacy_overdue_scheduled_invasion_is_voided_not_defeated(db_session):
    group = await _make_group(db_session)
    invasion = await _make_legacy_invasion(db_session, group, scheduled_at=SLOT)
    # Woken 45 minutes late: the first window passed before anyone could join.
    stats = await RecoveryService.recover_on_startup(
        db_session, now=SLOT + timedelta(minutes=45)
    )
    assert stats["invasions_voided"] == 1
    assert stats["invasions_started"] == 0
    persisted = await _reload(db_session, invasion.id)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome is None
    assert persisted.ended_at is not None
    assert persisted.rewards_settled_at is None  # no approved outcome, never settled
    repeat = await RecoveryService.recover_on_startup(
        db_session, now=SLOT + timedelta(minutes=45)
    )
    assert repeat["invasions_voided"] == 0


@pytest.mark.asyncio
async def test_legacy_active_battle_advances_one_transition_per_pass(db_session):
    group = await _make_group(db_session)
    invasion = await _make_legacy_invasion(db_session, group, scheduled_at=SLOT)
    await MaintenanceService.run_invasion_cycle(now=SLOT, schedule_occurrences=False)
    await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(minutes=10), schedule_occurrences=False
    )
    persisted = await _reload(db_session, invasion.id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.current_wave_sequence == 2
    # Legacy pass ordering: wave 2 activates on one pass, then finishes on the next.
    started_second = await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(minutes=20), schedule_occurrences=False
    )
    assert started_second["invasion_wave_transitions"] == 1
    stats = await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(minutes=20), schedule_occurrences=False
    )
    assert stats["invasions_resolved"] == 1
    persisted = await _reload(db_session, invasion.id)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome in InvasionOutcome.ALL
    assert _utc(persisted.ended_at) == SLOT + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_legacy_battle_is_never_interrupted_or_compensated_by_the_budget(db_session):
    group = await _make_group(db_session)
    invasion = await _make_legacy_invasion(db_session, group, scheduled_at=SLOT)
    invasion_id = int(invasion.id)
    await MaintenanceService.run_invasion_cycle(now=SLOT, schedule_occurrences=False)
    before = [
        _utc(wave.deadline_at) if wave.deadline_at else None
        for wave in await _waves_for(db_session, invasion_id)
    ]
    # Three hours without maintenance: far beyond the 20-minute budget. Legacy
    # events have no heartbeat, so this must neither interrupt nor shift deadlines.
    late = SLOT + timedelta(hours=3)
    interrupted = 0
    for _ in range(4):
        stats = await MaintenanceService.run_invasion_cycle(
            now=late, schedule_occurrences=False
        )
        interrupted += stats.get("invasions_interrupted", 0)
    assert interrupted == 0
    persisted = await _reload(db_session, invasion_id)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome in InvasionOutcome.ALL
    assert persisted.heartbeat_at is None
    after = [
        _utc(wave.deadline_at) if wave.deadline_at else None
        for wave in await _waves_for(db_session, invasion_id)
    ]
    # The wave scheduled before the outage keeps its deadline. Wave 2 was not yet
    # scheduled, so it only receives its own original legacy deadline when it runs.
    assert before[0] is not None and before[1] is None
    assert after[0] == before[0]


async def _waves_for(session, invasion_id: int):
    session.expire_all()
    return list(
        (
            await session.scalars(
                select(InvasionWave)
                .where(InvasionWave.invasion_id == invasion_id)
                .order_by(InvasionWave.wave_sequence.asc())
            )
        ).all()
    )


# ----------------------------------------------------------------------
# Scheduler and recovery integration
# ----------------------------------------------------------------------
def test_existing_maintenance_loop_drives_invasions_without_a_bot_argument():
    source = inspect.getsource(MaintenanceService)
    assert source.count("async def run_forever") == 1
    assert MaintenanceService.INTERVAL_SECONDS == 5
    assert "run_invasion_cycle" in source
    assert "bot" not in inspect.signature(MaintenanceService.run_invasion_cycle).parameters


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_maintenance_workers_create_one_occurrence(db_session):
    group = await _make_group(db_session, matches_played=12)
    now = SLOT + timedelta(minutes=1)
    results = await asyncio.gather(
        MaintenanceService._schedule_current_invasions(now),
        MaintenanceService._schedule_current_invasions(now),
    )
    occurrence_key = MaintenanceService._occurrence_key(group.id, int(SLOT.timestamp()))
    count = await db_session.scalar(
        select(func.count(Invasion.id)).where(Invasion.occurrence_key == occurrence_key)
    )
    assert count == 1
    assert sum(result["invasions_scheduled"] for result in results) == 1
    assert sum(result["invasion_failures"] for result in results) == 0


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_workers_resolve_one_battle_exactly_once(db_session):
    group = await _make_group(db_session)
    await MaintenanceService.run_invasion_cycle(now=SLOT + timedelta(minutes=1))
    invasion = (await _group_invasions(db_session, group.id))[0]
    final = SLOT + timedelta(minutes=20)
    await asyncio.gather(
        MaintenanceService.run_invasion_cycle(now=final, schedule_occurrences=False),
        MaintenanceService.run_invasion_cycle(now=final, schedule_occurrences=False),
    )
    persisted = await _reload(db_session, invasion.id)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome in InvasionOutcome.ALL
