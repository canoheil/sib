"""Integration tests for durable Invasion scheduling and restart recovery."""

import asyncio
import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
from sib.services.invasion import InvasionConfigurationError, InvasionService
from sib.services.maintenance.service import MaintenanceService
from sib.services.recovery import RecoveryService
from sib.tests.conftest import requires_row_locks

UTC = timezone.utc
BASE_TIME = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _make_group(
    session,
    *,
    matches_played: int = 0,
    is_active: bool = True,
) -> Group:
    group_id = -(10_000 + int(uuid.uuid4().int % 1_000_000_000))
    group = Group(
        id=group_id,
        title=f"Invasion schedule test {group_id}",
        is_active=is_active,
    )
    session.add(group)
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
    return group


async def _make_scheduled_invasion(
    session,
    group: Group,
    *,
    scheduled_at: datetime = BASE_TIME,
    occurrence_key: str | None = None,
    matches_played: int = 0,
) -> Invasion:
    slot_epoch = int(scheduled_at.timestamp())
    enemy_power, event_config, event_spec, wave_compositions = (
        MaintenanceService._build_invasion_snapshot(
            group_id=group.id,
            matches_played=matches_played,
            slot_epoch=slot_epoch,
            scheduled_at=scheduled_at,
            interval_seconds=4 * 60 * 60,
            duration_seconds=20 * 60,
        )
    )
    invasion = await InvasionService.create(
        session,
        group_id=group.id,
        occurrence_key=occurrence_key or f"test-invasion:{uuid.uuid4().hex}",
        scheduled_at=scheduled_at,
        enemy_faction_code=MaintenanceService.INVASION_ENEMY_FACTION,
        enemy_power=enemy_power,
        event_config=event_config,
        event_spec=event_spec,
        wave_compositions=wave_compositions,
    )
    await session.commit()
    return invasion


async def _reload_invasion(session, invasion_id: int) -> Invasion:
    session.expire_all()
    invasion = await session.get(Invasion, invasion_id)
    assert invasion is not None
    return invasion


@pytest.mark.asyncio
async def test_scheduler_uses_active_groups_stable_utc_slots_and_frozen_specs(db_session):
    quiet_group = await _make_group(db_session, matches_played=0)
    active_group = await _make_group(db_session, matches_played=25)
    inactive_group = await _make_group(
        db_session,
        matches_played=90,
        is_active=False,
    )

    quiet_group_id = quiet_group.id
    active_group_id = active_group.id
    inactive_group_id = inactive_group.id
    now = BASE_TIME + timedelta(minutes=1)
    first = await MaintenanceService.run_invasion_cycle(now=now)

    assert first["invasions_scheduled"] == 2
    assert first["invasions_started"] == 2
    assert first["invasion_failures"] == 0
    assert CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"] == 4 * 60 * 60
    assert CONFIG_DEFAULTS["invasion_event_duration_seconds"] == 20 * 60

    db_session.expire_all()
    invasions = list(
        (
            await db_session.scalars(
                select(Invasion).order_by(Invasion.group_id.asc())
            )
        ).all()
    )
    assert len(invasions) == 2
    by_group = {invasion.group_id: invasion for invasion in invasions}
    assert set(by_group) == {quiet_group_id, active_group_id}
    assert inactive_group_id not in by_group

    for group_id, expected_power in ((quiet_group_id, 100), (active_group_id, 125)):
        invasion = by_group[group_id]
        assert invasion.status == InvasionStatus.ACTIVE
        assert invasion.occurrence_key == MaintenanceService._occurrence_key(
            group_id, int(BASE_TIME.timestamp())
        )
        assert invasion.enemy_power == expected_power
        config = json.loads(invasion.event_config_json)
        assert config["schema_version"] == 1
        assert config["occurrence_interval_seconds"] == 4 * 60 * 60
        assert config["duration_seconds"] == 20 * 60
        assert config["enemy_power_base"] == 100
        assert config["enemy_power_activity_bonus_cap"] == 100
        assert config["enemy_power_rule"] == (
            "base_100_plus_matches_played_capped_at_100_v1"
        )
        assert config["group_matches_played_snapshot"] == (
            0 if group_id == quiet_group_id else 25
        )
        assert config["wave_deadlines_utc"] == [
            "2026-10-07T08:10:00Z",
            "2026-10-07T08:20:00Z",
        ]
        assert config["planned_end_at_utc"] == "2026-10-07T08:20:00Z"
        assert invasion.current_wave_sequence == 1

        waves = list(
            (
                await db_session.scalars(
                    select(InvasionWave)
                    .where(InvasionWave.invasion_id == invasion.id)
                    .order_by(InvasionWave.wave_sequence.asc())
                )
            ).all()
        )
        assert len(waves) == 2
        wave_specs = [json.loads(wave.enemy_composition_json) for wave in waves]
        assert [spec["wave_sequence"] for spec in wave_specs] == [1, 2]
        assert all(
            spec["enemy_faction_code"] == "moon_descendants"
            for spec in wave_specs
        )
        assert sum(spec["enemy_power"] for spec in wave_specs) == expected_power

    await db_session.commit()
    repeated = await MaintenanceService.run_invasion_cycle(now=now)
    assert repeated["invasions_scheduled"] == 0
    assert repeated["duplicate_invasion_occurrences"] == 2
    assert repeated["invasions_started"] == 0
    assert repeated["invasion_wave_transitions"] == 0
    assert await db_session.scalar(select(func.count(Invasion.id))) == 2


@pytest.mark.asyncio
async def test_configured_cadence_and_duration_are_snapshotted_per_occurrence(db_session):
    group = await _make_group(db_session)
    await ConfigService.set(
        db_session,
        "invasion_occurrence_interval_seconds",
        6 * 60 * 60,
    )
    await ConfigService.set(db_session, "invasion_event_duration_seconds", 10 * 60)
    await db_session.commit()

    now = datetime(2026, 10, 7, 9, 15, tzinfo=UTC)
    stats = await MaintenanceService.run_invasion_cycle(now=now)
    assert stats["invasions_scheduled"] == 1

    invasion = await db_session.scalar(
        select(Invasion).where(Invasion.group_id == group.id)
    )
    assert invasion is not None
    assert _utc(invasion.scheduled_at) == datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    config = json.loads(invasion.event_config_json)
    assert config["occurrence_interval_seconds"] == 6 * 60 * 60
    assert config["duration_seconds"] == 10 * 60
    assert config["wave_durations_seconds"] == [300, 300]
    assert config["wave_deadlines_utc"] == [
        "2026-10-07T12:05:00Z",
        "2026-10-07T12:10:00Z",
    ]


@pytest.mark.asyncio
async def test_maintenance_advances_due_waves_and_resolves_from_persisted_totals(db_session):
    group = await _make_group(db_session)

    started = await MaintenanceService.run_invasion_cycle(now=BASE_TIME)
    assert started["invasions_started"] == 1
    invasion = await db_session.scalar(
        select(Invasion).where(Invasion.group_id == group.id)
    )
    assert invasion is not None
    invasion_id = invasion.id

    before_deadline = await MaintenanceService.run_invasion_cycle(
        now=BASE_TIME + timedelta(minutes=9, seconds=59)
    )
    assert before_deadline["invasion_wave_transitions"] == 0
    invasion = await _reload_invasion(db_session, invasion_id)
    assert invasion.current_wave_sequence == 1

    first_wave_end = BASE_TIME + timedelta(minutes=10)
    finished_first = await MaintenanceService.run_invasion_cycle(now=first_wave_end)
    assert finished_first["invasion_wave_transitions"] == 1
    invasion = await _reload_invasion(db_session, invasion_id)
    first_wave = await InvasionService.get_current_wave(db_session, invasion_id)
    assert first_wave is not None
    assert first_wave.wave_sequence == 2
    assert first_wave.status == InvasionStatus.SCHEDULED
    assert _utc(first_wave.next_transition_at) == first_wave_end
    assert invasion.status == InvasionStatus.ACTIVE

    started_second = await MaintenanceService.run_invasion_cycle(now=first_wave_end)
    assert started_second["invasion_wave_transitions"] == 1
    second_wave = await InvasionService.get_current_wave(db_session, invasion_id)
    assert second_wave is not None
    assert second_wave.status == InvasionStatus.ACTIVE
    assert _utc(second_wave.deadline_at) == BASE_TIME + timedelta(minutes=20)

    completed = await MaintenanceService.run_invasion_cycle(
        now=BASE_TIME + timedelta(minutes=20)
    )
    assert completed["invasion_wave_transitions"] == 1
    assert completed["invasions_resolved"] == 1
    invasion = await _reload_invasion(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome == InvasionOutcome.DEFEAT
    assert _utc(invasion.ended_at) == BASE_TIME + timedelta(minutes=20)

    await db_session.commit()
    replay = await MaintenanceService.run_invasion_cycle(
        now=BASE_TIME + timedelta(minutes=20)
    )
    assert replay["invasion_wave_transitions"] == 0
    assert replay["invasions_resolved"] == 0
    invasion = await _reload_invasion(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert _utc(invasion.ended_at) == BASE_TIME + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_recovery_catches_up_overdue_event_one_transition_per_startup(db_session):
    group = await _make_group(db_session)
    invasion = await _make_scheduled_invasion(db_session, group)
    overdue_now = BASE_TIME + timedelta(minutes=45)

    recovered_start = await RecoveryService.recover_on_startup(
        db_session,
        now=overdue_now,
    )
    assert recovered_start["invasions_started"] == 1
    assert recovered_start["invasion_wave_transitions"] == 0
    invasion = await _reload_invasion(db_session, invasion.id)
    assert invasion.status == InvasionStatus.ACTIVE
    first_wave = await InvasionService.get_current_wave(db_session, invasion.id)
    assert first_wave is not None
    assert first_wave.status == InvasionStatus.ACTIVE
    assert _utc(first_wave.deadline_at) == BASE_TIME + timedelta(minutes=10)

    recovered_finish_first = await RecoveryService.recover_on_startup(
        db_session,
        now=overdue_now,
    )
    assert recovered_finish_first["invasion_wave_transitions"] == 1
    assert recovered_finish_first["invasions_resolved"] == 0
    invasion = await _reload_invasion(db_session, invasion.id)
    assert invasion.current_wave_sequence == 2

    recovered_start_second = await RecoveryService.recover_on_startup(
        db_session,
        now=overdue_now,
    )
    assert recovered_start_second["invasion_wave_transitions"] == 1
    second_wave = await InvasionService.get_current_wave(db_session, invasion.id)
    assert second_wave is not None
    assert second_wave.status == InvasionStatus.ACTIVE
    assert _utc(second_wave.deadline_at) == BASE_TIME + timedelta(minutes=20)

    recovered_resolution = await RecoveryService.recover_on_startup(
        db_session,
        now=overdue_now,
    )
    assert recovered_resolution["invasion_wave_transitions"] == 1
    assert recovered_resolution["invasions_resolved"] == 1
    invasion = await _reload_invasion(db_session, invasion.id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome == InvasionOutcome.DEFEAT

    ended_at = _utc(invasion.ended_at)
    repeat = await RecoveryService.recover_on_startup(db_session, now=overdue_now)
    assert repeat["invasions_started"] == 0
    assert repeat["invasion_wave_transitions"] == 0
    assert repeat["invasions_resolved"] == 0
    invasion = await _reload_invasion(db_session, invasion.id)
    assert _utc(invasion.ended_at) == ended_at


@pytest.mark.asyncio
async def test_future_scheduled_invasion_is_not_started_early(db_session):
    group = await _make_group(db_session)
    scheduled_at = BASE_TIME + timedelta(hours=1)
    invasion = await _make_scheduled_invasion(
        db_session,
        group,
        scheduled_at=scheduled_at,
    )

    early = await MaintenanceService.run_invasion_cycle(
        now=scheduled_at - timedelta(seconds=1),
        schedule_occurrences=False,
    )
    assert early["invasions_started"] == 0
    assert (await _reload_invasion(db_session, invasion.id)).status == InvasionStatus.SCHEDULED

    due = await MaintenanceService.run_invasion_cycle(
        now=scheduled_at,
        schedule_occurrences=False,
    )
    assert due["invasions_started"] == 1
    assert (await _reload_invasion(db_session, invasion.id)).status == InvasionStatus.ACTIVE


@pytest.mark.asyncio
async def test_normal_domain_call_still_rejects_an_unapproved_past_deadline(db_session):
    group = await _make_group(db_session)
    invasion = await _make_scheduled_invasion(db_session, group)

    with pytest.raises(InvasionConfigurationError):
        await InvasionService.start(
            db_session,
            invasion.id,
            now=BASE_TIME,
            deadline_at=BASE_TIME - timedelta(seconds=1),
        )
    assert invasion.status == InvasionStatus.SCHEDULED


@pytest.mark.asyncio
async def test_failure_for_one_invasion_does_not_block_another(db_session, caplog):
    group_a = await _make_group(db_session)
    group_b = await _make_group(db_session)
    invasion_a = await _make_scheduled_invasion(db_session, group_a)
    invasion_b = await _make_scheduled_invasion(db_session, group_b)
    invasion_a_id = invasion_a.id
    invasion_b_id = invasion_b.id
    group_a_id = group_a.id
    original_start = InvasionService.start

    async def fail_only_one(
        session,
        invasion_id,
        *,
        now=None,
        deadline_at=None,
        allow_overdue_deadline=False,
    ):
        if invasion_id == invasion_a_id:
            raise RuntimeError("injected Invasion start failure")
        return await original_start(
            session,
            invasion_id,
            now=now,
            deadline_at=deadline_at,
            allow_overdue_deadline=allow_overdue_deadline,
        )

    with patch.object(InvasionService, "start", new=fail_only_one):
        stats = await MaintenanceService.run_invasion_cycle(
            now=BASE_TIME + timedelta(minutes=1),
            schedule_occurrences=False,
        )

    assert stats["invasion_failures"] == 1
    assert stats["invasions_started"] == 1
    assert (await _reload_invasion(db_session, invasion_a_id)).status == InvasionStatus.SCHEDULED
    assert (await _reload_invasion(db_session, invasion_b_id)).status == InvasionStatus.ACTIVE
    assert f"invasion_id={invasion_a_id}" in caplog.text
    assert f"group_id={group_a_id}" in caplog.text


@pytest.mark.asyncio
async def test_existing_maintenance_loop_drives_invasions_without_a_bot_argument():
    source = inspect.getsource(MaintenanceService)
    assert source.count("async def run_forever") == 1
    assert MaintenanceService.INTERVAL_SECONDS == 5
    assert "run_invasion_cycle" in source
    assert not hasattr(MaintenanceService, "run_invasion_forever")
    assert "bot" not in inspect.signature(
        MaintenanceService.run_invasion_cycle
    ).parameters


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_maintenance_workers_create_one_occurrence(db_session):
    group = await _make_group(db_session, matches_played=12)
    await db_session.commit()
    now = BASE_TIME + timedelta(minutes=1)

    results = await asyncio.gather(
        MaintenanceService._schedule_current_invasions(now),
        MaintenanceService._schedule_current_invasions(now),
    )
    occurrence_key = MaintenanceService._occurrence_key(
        group.id, int(BASE_TIME.timestamp())
    )
    count = await db_session.scalar(
        select(func.count(Invasion.id)).where(
            Invasion.occurrence_key == occurrence_key
        )
    )
    assert count == 1
    assert sum(result["invasions_scheduled"] for result in results) == 1
    assert sum(result["invasion_failures"] for result in results) == 0


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_workers_advance_one_persisted_wave_safely(db_session):
    group = await _make_group(db_session)
    invasion = await _make_scheduled_invasion(db_session, group)
    invasion_id = invasion.id

    starts = await asyncio.gather(
        MaintenanceService.run_invasion_cycle(
            now=BASE_TIME,
            schedule_occurrences=False,
        ),
        MaintenanceService.run_invasion_cycle(
            now=BASE_TIME,
            schedule_occurrences=False,
        ),
    )
    assert sum(result["invasions_started"] for result in starts) >= 1
    persisted = await _reload_invasion(db_session, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.current_wave_sequence == 1

    first_deadline = BASE_TIME + timedelta(minutes=10)
    await asyncio.gather(
        MaintenanceService.run_invasion_cycle(
            now=first_deadline,
            schedule_occurrences=False,
        ),
        MaintenanceService.run_invasion_cycle(
            now=first_deadline,
            schedule_occurrences=False,
        ),
    )
    persisted = await _reload_invasion(db_session, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.current_wave_sequence in (2, None)

    # Let any second-wave start/finish that is due complete through the same
    # row-locked service path; a repeated resolution must preserve one result.
    final_deadline = BASE_TIME + timedelta(minutes=20)
    for _ in range(3):
        await MaintenanceService.run_invasion_cycle(
            now=final_deadline,
            schedule_occurrences=False,
        )
    persisted = await _reload_invasion(db_session, invasion_id)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome in InvasionOutcome.ALL


def test_default_cadence_is_four_hours_with_six_utc_aligned_slots():
    interval = int(CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"])
    duration = int(CONFIG_DEFAULTS["invasion_event_duration_seconds"])
    assert interval == 4 * 60 * 60
    assert duration == 20 * 60

    day = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    slot_epochs = []
    for slot_index in range(6):
        now = day + timedelta(hours=4 * slot_index, minutes=5)
        slot_epoch, scheduled_at = MaintenanceService._slot_start(
            now, interval, duration
        )
        assert scheduled_at == day + timedelta(hours=4 * slot_index)
        slot_epochs.append(slot_epoch)

    # Exactly six UTC-aligned slots per day: 00:00, 04:00, ..., 20:00.
    assert [
        datetime.fromtimestamp(epoch, tz=UTC) for epoch in slot_epochs
    ] == [day + timedelta(hours=4 * index) for index in range(6)]
    assert len(set(slot_epochs)) == 6

    # Occurrence keys remain unique across the six daily slots.
    keys = {
        MaintenanceService._occurrence_key(-100700, epoch) for epoch in slot_epochs
    }
    assert len(keys) == 6


@pytest.mark.asyncio
async def test_default_invasion_timing_config_is_four_hour_cadence(db_session):
    interval, duration = await MaintenanceService._invasion_timing_config(db_session)
    assert interval == 4 * 60 * 60
    assert duration == 20 * 60


@pytest.mark.asyncio
async def test_scheduler_creates_one_occurrence_per_four_hour_slot_per_day(db_session):
    group = await _make_group(db_session)
    group_id = group.id
    day = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    for slot_index in range(6):
        stats = await MaintenanceService.run_invasion_cycle(
            now=day + timedelta(hours=4 * slot_index, minutes=5)
        )
        assert stats["invasions_scheduled"] == 1
        assert stats["invasion_failures"] == 0

    db_session.expire_all()
    invasions = list(
        (
            await db_session.scalars(
                select(Invasion)
                .where(Invasion.group_id == group_id)
                .order_by(Invasion.scheduled_at.asc())
            )
        ).all()
    )
    assert len(invasions) == 6
    assert [_utc(invasion.scheduled_at) for invasion in invasions] == [
        day + timedelta(hours=4 * index) for index in range(6)
    ]
    occurrence_keys = [invasion.occurrence_key for invasion in invasions]
    assert len(set(occurrence_keys)) == 6
    for invasion in invasions:
        config = json.loads(invasion.event_config_json)
        assert config["occurrence_interval_seconds"] == 4 * 60 * 60
        assert config["duration_seconds"] == 20 * 60


@pytest.mark.asyncio
async def test_missed_slots_are_not_backfilled_after_an_outage(db_session):
    group = await _make_group(db_session)
    group_id = group.id
    # The 08:00 slot window (20 minutes) has fully passed when the bot wakes
    # up: the scheduler must target the NEXT 4-hour slot instead of backfilling
    # the missed one with an already-expired event.
    now = datetime(2026, 10, 7, 8, 25, tzinfo=UTC)
    stats = await MaintenanceService.run_invasion_cycle(now=now)

    assert stats["invasions_scheduled"] == 1
    assert stats["invasions_started"] == 0
    assert stats["invasion_failures"] == 0

    db_session.expire_all()
    invasion = await db_session.scalar(
        select(Invasion).where(Invasion.group_id == group_id)
    )
    assert invasion is not None
    assert _utc(invasion.scheduled_at) == datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert invasion.status == InvasionStatus.SCHEDULED
    assert invasion.occurrence_key == MaintenanceService._occurrence_key(
        group_id, int(datetime(2026, 10, 7, 12, 0, tzinfo=UTC).timestamp())
    )
    missed_key = MaintenanceService._occurrence_key(
        group_id, int(datetime(2026, 10, 7, 8, 0, tzinfo=UTC).timestamp())
    )
    assert (
        await db_session.scalar(
            select(func.count(Invasion.id)).where(
                Invasion.occurrence_key == missed_key
            )
        )
        == 0
    )


@pytest.mark.asyncio
async def test_restart_and_recovery_do_not_create_duplicate_invasions(db_session):
    group = await _make_group(db_session)
    group_id = group.id
    now = datetime(2026, 10, 7, 8, 5, tzinfo=UTC)

    first = await MaintenanceService.run_invasion_cycle(now=now)
    assert first["invasions_scheduled"] == 1
    assert first["invasions_started"] == 1

    # A restart replays the same maintenance cycle: the occurrence key keeps
    # exactly one Invasion per Group and slot.
    second = await MaintenanceService.run_invasion_cycle(now=now)
    assert second["invasions_scheduled"] == 0
    assert second["duplicate_invasion_occurrences"] == 1
    assert second["invasions_started"] == 0

    # Startup recovery replays transitions but never schedules duplicates.
    recovered = await RecoveryService.recover_on_startup(db_session, now=now)
    assert recovered["invasions_scheduled"] == 0
    assert recovered["duplicate_invasion_occurrences"] == 0

    db_session.expire_all()
    assert (
        await db_session.scalar(
            select(func.count(Invasion.id)).where(Invasion.group_id == group_id)
        )
        == 1
    )
    invasion = await db_session.scalar(
        select(Invasion).where(Invasion.group_id == group_id)
    )
    assert invasion.status == InvasionStatus.ACTIVE
    assert invasion.occurrence_key == MaintenanceService._occurrence_key(
        group_id, int(datetime(2026, 10, 7, 8, 0, tzinfo=UTC).timestamp())
    )
