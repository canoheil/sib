"""Four-wave Invasion battles: activation, timeline, participation, defense,
outage recovery, settlement, and Telegram presentation.

Heartbeat rule under test: maintenance (and every battle action) refreshes the
heartbeat. A gap of up to ``HEARTBEAT_TOLERANCE_SECONDS`` (30 s) is ordinary
cadence jitter. Anything longer is downtime and is restored to the active
wave. Tests that span a battle therefore tick at live cadence (``_live``, one
tick every 15 s), exactly as the production loop does, unless the test is
about downtime itself.

Row-lock races are MySQL/MariaDB-only and live in ``test_invasion_maintenance_recovery``.
"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    Invasion,
    InvasionContribution,
    InvasionDefenseAction,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
    UserItem,
)
from sib.services import invasion_telegram
from sib.services.config_store import ConfigService
from sib.services.invasion import (
    InvasionAlreadyResolvedError,
    InvasionDefenseActionAlreadyUsedError,
    InvasionDefenseActionCode,
    InvasionJoinClosedError,
    InvasionNotParticipantError,
    InvasionService,
    InvasionStaleWaveError,
    outcome_for,
)
from sib.services.invasion_rewards import GOLDEN_APPLE_ITEM_KEY
from sib.services.invasion_telegram import (
    InvasionTelegramService,
    render_final_notice,
    render_invasion_status,
    render_wave_notice,
)
from sib.services.maintenance.service import InvasionBattleConfig, MaintenanceService
from sib.services.recovery import RecoveryService
from sib.tests.test_parties import create_test_group, create_test_user

UTC = timezone.utc
SLOT = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)  # a 4-hour UTC slot start
WAVE = 300  # default seconds per wave


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def at(seconds: float) -> datetime:
    return SLOT + timedelta(seconds=seconds)


async def _live(start: float, end: float, step: int = 15) -> Counter:
    """Tick the maintenance cycle at live cadence; return the summed stats."""
    totals: Counter = Counter()
    current = start
    while current <= end:
        stats = await MaintenanceService.run_invasion_cycle(
            now=at(current), schedule_occurrences=False
        )
        totals.update(stats)
        current += step
    return totals


async def _activate(session, group_id: int, now: datetime) -> int:
    await MaintenanceService.run_invasion_cycle(now=now)
    session.expire_all()
    invasion = await session.scalar(
        select(Invasion)
        .where(Invasion.group_id == group_id)
        .order_by(Invasion.id.desc())
    )
    assert invasion is not None, "expected a battle to activate inside its window"
    return int(invasion.id)


async def _load(session, invasion_id: int) -> Invasion:
    session.expire_all()
    invasion = await session.get(Invasion, invasion_id)
    assert invasion is not None
    return invasion


async def _waves(session, invasion_id: int) -> list[InvasionWave]:
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


async def _group_id(session) -> int:
    group = await create_test_group(session, "Four-wave battle group")
    gid = int(group.id)
    await session.commit()
    return gid


async def _user_id(session, name: str) -> int:
    user = await create_test_user(session, name)
    uid = int(user.id)
    await session.commit()
    return uid


async def _defended_battle(session) -> tuple[int, int, int]:
    """Activate a battle, let one player brace in wave 1, and stop at 10 s."""
    group_id = await _group_id(session)
    invasion_id = await _activate(session, group_id, at(0))
    defender = await _user_id(session, "Brace presenter")
    await InvasionService.join(session, invasion_id, defender, expected_group_id=group_id, now=at(1))
    await InvasionService.perform_defense_action(
        session,
        invasion_id,
        defender,
        expected_group_id=group_id,
        wave_sequence=1,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key="presenter-brace",
        now=at(10),
    )
    await session.commit()
    return group_id, invasion_id, defender


async def _group_invasion(session, group_id: int) -> Invasion:
    session.expire_all()
    return await session.scalar(select(Invasion).where(Invasion.group_id == group_id))


# ----------------------------------------------------------------------
# Activation and the four-wave timeline
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_battle_activates_at_slot_start_with_four_five_minute_waves(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))

    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.ACTIVE
    assert _utc(invasion.started_at) == at(0)
    assert _utc(invasion.scheduled_at) == at(0)
    assert invasion.current_wave_sequence == 1
    assert _utc(invasion.next_transition_at) == at(WAVE)
    assert invasion.presentation_stage == 0
    assert _utc(invasion.heartbeat_at) == at(0)
    config = json.loads(invasion.event_config_json)
    assert config["schema_version"] == 2
    assert config["wave_count"] == 4
    assert config["wave_duration_seconds"] == WAVE

    waves = await _waves(db_session, invasion_id)
    assert [wave.wave_sequence for wave in waves] == [1, 2, 3, 4]
    assert waves[0].status == InvasionStatus.ACTIVE
    assert _utc(waves[0].deadline_at) == at(WAVE)
    assert all(wave.status == InvasionStatus.SCHEDULED for wave in waves[1:])
    assert all(wave.deadline_at is None for wave in waves[1:])


@pytest.mark.asyncio
async def test_activation_time_is_the_real_tick_not_the_slot_start(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(7))
    invasion = await _load(db_session, invasion_id)
    assert _utc(invasion.scheduled_at) == at(0)
    assert _utc(invasion.started_at) == at(7)
    waves = await _waves(db_session, invasion_id)
    assert _utc(waves[0].deadline_at) == at(7 + WAVE)


@pytest.mark.asyncio
async def test_wave_boundaries_are_exact_and_the_battle_ends_at_twenty_minutes(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))

    await _live(15, 285)
    assert (await _load(db_session, invasion_id)).current_wave_sequence == 1

    await _live(300, 300)
    assert (await _load(db_session, invasion_id)).current_wave_sequence == 2
    waves = await _waves(db_session, invasion_id)
    assert _utc(waves[1].started_at) == at(300)
    assert _utc(waves[1].deadline_at) == at(600)

    await _live(315, 900)
    assert (await _load(db_session, invasion_id)).current_wave_sequence == 4

    await _live(915, 1185)
    assert (await _load(db_session, invasion_id)).status == InvasionStatus.ACTIVE

    await _live(1200, 1200)
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert _utc(invasion.ended_at) == at(1200)
    assert invasion.current_wave_sequence is None
    waves = await _waves(db_session, invasion_id)
    assert [_utc(wave.ended_at) for wave in waves] == [at(300), at(600), at(900), at(1200)]
    assert all(wave.status == InvasionStatus.FINISHED for wave in waves)


@pytest.mark.asyncio
async def test_late_tick_within_tolerance_keeps_the_wave_grid(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 285)  # last heartbeat at 285

    # The 300 s boundary is observed 25 s late: still ordinary jitter (<= 30 s).
    stats = await MaintenanceService.run_invasion_cycle(
        now=at(310), schedule_occurrences=False
    )
    assert stats["invasion_heartbeat_restores"] == 0
    assert stats["invasion_wave_transitions"] == 1
    waves = await _waves(db_session, invasion_id)
    assert _utc(waves[1].started_at) == at(300)  # the grid, not 310
    assert _utc(waves[1].deadline_at) == at(600)


@pytest.mark.asyncio
async def test_gap_longer_than_a_wave_is_downtime_restored_not_cascaded(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    # No maintenance ran for 910 s. The stall is downtime: wave 1 keeps the
    # 300 s it had at its last heartbeat (less the 30 s tolerance), and no
    # wave is skipped or finished by the stall itself.
    stats = await MaintenanceService.run_invasion_cycle(
        now=at(910), schedule_occurrences=False
    )
    assert stats["invasion_heartbeat_restores"] == 1
    assert stats["invasion_wave_transitions"] == 0
    waves = await _waves(db_session, invasion_id)
    assert waves[0].status == InvasionStatus.ACTIVE
    assert _utc(waves[0].deadline_at) == at(300 + 880)  # 910 - 30 tolerance
    assert (_utc(waves[0].deadline_at) - at(910)).total_seconds() == 270


@pytest.mark.asyncio
async def test_stale_clock_never_moves_the_heartbeat_backwards(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    # A fresh tick at 100 s, then a worker that read the clock earlier (50 s)
    # runs afterwards. It must not rewind the heartbeat.
    await _live(15, 90)  # last live heartbeat at 90 s
    await MaintenanceService.run_invasion_cycle(now=at(50), schedule_occurrences=False)
    invasion = await _load(db_session, invasion_id)
    assert _utc(invasion.heartbeat_at) == at(90)
    # The next live tick sees a 30 s gap, which is jitter: no restoration.
    stats = await MaintenanceService.run_invasion_cycle(now=at(120), schedule_occurrences=False)
    assert stats["invasion_heartbeat_restores"] == 0
    assert _utc((await _waves(db_session, invasion_id))[0].deadline_at) == at(WAVE)


@pytest.mark.asyncio
async def test_final_resolution_happens_exactly_once(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    first = await _live(15, 1200)
    assert first["invasions_resolved"] == 1
    later = await MaintenanceService.run_invasion_cycle(
        now=at(1300), schedule_occurrences=False
    )
    assert later["invasions_resolved"] == 0
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert _utc(invasion.ended_at) == at(1200)


@pytest.mark.asyncio
async def test_empty_battle_completes_as_defeat_without_participants_or_rewards(db_session):
    """Four timed waves can finish empty; that is a defeat, not an interruption."""
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))

    stats = await _live(15, 1200)

    assert stats["invasions_resolved"] == 1
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome == InvasionOutcome.DEFEAT
    assert _utc(invasion.ended_at) == at(1200)
    assert invasion.rewards_settled_at is not None
    waves = await _waves(db_session, invasion_id)
    assert [wave.status for wave in waves] == [InvasionStatus.FINISHED] * 4
    assert [wave.contribution_total for wave in waves] == [0, 0, 0, 0]
    assert [
        _utc(wave.ended_at) for wave in waves
    ] == [at(WAVE * sequence) for sequence in range(1, 5)]
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 0

    snapshot = await InvasionTelegramService.load_snapshot(invasion_id)
    assert snapshot is not None
    status_text = render_invasion_status(snapshot, now=at(1200))
    result_text = render_final_notice(snapshot)
    assert "موج‌ها: ۴ از ۴ تکمیل شد" in status_text
    assert "نتیجه: شکست در دفاع" in status_text
    assert "شرکت‌کنندگان: ۰" in status_text
    assert "دفاع ثبت‌شده: ۰" in status_text
    assert "پاداشی ثبت نشد؛ مدافع واجد شرایطی وجود نداشت" in status_text
    assert "نتیجه: شکست در دفاع" in result_text
    assert "پاداشی ثبت نشد؛ مدافع واجد شرایطی وجود نداشت" in result_text


@pytest.mark.asyncio
async def test_live_cadence_never_mistakes_jitter_for_downtime(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    totals = await _live(5, 290, step=5)
    assert totals["invasion_heartbeat_restores"] == 0
    waves = await _waves(db_session, invasion_id)
    assert _utc(waves[0].deadline_at) == at(WAVE)


# ----------------------------------------------------------------------
# Participation and defense
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_join_once_and_joining_alone_adds_no_defense(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Joiner once")

    first = await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1)
    )
    second = await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(2)
    )
    await db_session.commit()
    assert first.id == second.id
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id
        )
    ) == 1
    persisted = await db_session.get(InvasionParticipant, first.id)
    assert persisted.contribution_total == 0
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 0


@pytest.mark.asyncio
async def test_join_is_open_through_the_final_wave_and_closes_at_battle_end(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    early_user = await _user_id(db_session, "Joins in wave three")
    closing_user = await _user_id(db_session, "Joins after the end")

    await _live(15, 650)
    await InvasionService.join(
        db_session, invasion_id, early_user, expected_group_id=group_id, now=at(650)
    )
    await db_session.commit()

    await _live(665, 1200)
    with pytest.raises(InvasionJoinClosedError):
        await InvasionService.join(
            db_session, invasion_id, closing_user, expected_group_id=group_id, now=at(1200)
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_join_after_a_silent_stall_restores_the_battle_instead_of_closing(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Joins after silence")
    # Nothing ran for 1205 s. The join restores downtime first, so participation
    # is still open: a stall must not close a battle that players never had a
    # chance to finish.
    await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1205)
    )
    await db_session.commit()
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.ACTIVE
    assert invasion.current_wave_sequence == 1


@pytest.mark.asyncio
async def test_one_defense_per_wave_and_successive_waves_allowed(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Defends every wave")
    await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1)
    )
    await db_session.commit()

    async def brace(wave: int, key: str, moment: float):
        async with get_async_sessionmaker()() as session:
            result = await InvasionService.perform_defense_action(
                session,
                invasion_id,
                user_id,
                expected_group_id=group_id,
                wave_sequence=wave,
                action_code=InvasionDefenseActionCode.BRACE,
                idempotency_key=key,
                now=at(moment),
            )
            await session.commit()
            return result

    assert (await brace(1, "wave-1-first", 10)).contribution_amount == 1
    with pytest.raises(InvasionDefenseActionAlreadyUsedError):
        await brace(1, "wave-1-second-tap", 20)

    await _live(25, 310)
    assert (await brace(2, "wave-2", 310)).wave_total_after == 1
    await _live(325, 610)
    assert (await brace(3, "wave-3", 610)).wave_total_after == 1
    await _live(625, 910)
    assert (await brace(4, "wave-4", 910)).wave_total_after == 1

    async with get_async_sessionmaker()() as session:
        participant = await session.scalar(
            select(InvasionParticipant).where(
                InvasionParticipant.invasion_id == invasion_id,
                InvasionParticipant.user_id == user_id,
            )
        )
        assert participant.contribution_total == 4
        assert await session.scalar(
            select(func.count(InvasionDefenseAction.id)).where(
                InvasionDefenseAction.invasion_id == invasion_id
            )
        ) == 4


@pytest.mark.asyncio
async def test_replayed_callback_returns_the_original_result_without_new_contribution(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Replay")
    await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1)
    )
    first = await InvasionService.perform_defense_action(
        db_session,
        invasion_id,
        user_id,
        expected_group_id=group_id,
        wave_sequence=1,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key="telegram:brace:same-query",
        now=at(10),
    )
    await db_session.commit()
    replay = await InvasionService.perform_defense_action(
        db_session,
        invasion_id,
        user_id,
        expected_group_id=group_id,
        wave_sequence=1,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key="telegram:brace:same-query",
        now=at(11),
    )
    assert replay.duplicate is True
    assert replay.action_id == first.action_id
    assert replay.wave_total_after == first.wave_total_after


@pytest.mark.asyncio
async def test_expired_wave_rejects_a_stale_callback(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Late presser")
    await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1)
    )
    await db_session.commit()
    await _live(15, 300)  # wave 1 ends at 300; wave 2 is now current
    with pytest.raises(InvasionStaleWaveError):
        await InvasionService.perform_defense_action(
            db_session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            wave_sequence=1,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key="stale-wave-1",
            now=at(305),
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_brace_after_the_battle_ends_is_rejected(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Too late to defend")
    await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1)
    )
    await db_session.commit()
    await _live(15, 1200)
    with pytest.raises(InvasionAlreadyResolvedError):
        await InvasionService.perform_defense_action(
            db_session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            wave_sequence=4,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key="after-end",
            now=at(1200),
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_database_rule_is_the_backstop_for_simultaneous_callbacks(db_session, monkeypatch):
    """Two callbacks that both pass the locked pre-check: the second must fail.

    The pre-check is forced to miss, as it would for a racing transaction on
    MySQL. The database uniqueness rule must then reject the second row.
    """
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    user_id = await _user_id(db_session, "Racing callbacks")
    await InvasionService.join(
        db_session, invasion_id, user_id, expected_group_id=group_id, now=at(1)
    )
    await InvasionService.perform_defense_action(
        db_session,
        invasion_id,
        user_id,
        expected_group_id=group_id,
        wave_sequence=1,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key="race-winner",
        now=at(10),
    )
    await db_session.commit()

    async def no_prior_action(*args, **kwargs):
        return None

    monkeypatch.setattr(
        InvasionService, "_locked_wave_defense_action", staticmethod(no_prior_action)
    )
    async with get_async_sessionmaker()() as session:
        with pytest.raises(InvasionDefenseActionAlreadyUsedError):
            await InvasionService.perform_defense_action(
                session,
                invasion_id,
                user_id,
                expected_group_id=group_id,
                wave_sequence=1,
                action_code=InvasionDefenseActionCode.BRACE,
                idempotency_key="race-loser",
                now=at(11),
            )
        await session.rollback()

    async with get_async_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count(InvasionDefenseAction.id))
            .join(InvasionWave, InvasionWave.id == InvasionDefenseAction.wave_id)
            .where(
                InvasionDefenseAction.invasion_id == invasion_id,
                InvasionWave.wave_sequence == 1,
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_non_participant_cannot_defend(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    stranger = await _user_id(db_session, "Never joined")
    with pytest.raises(InvasionNotParticipantError):
        await InvasionService.perform_defense_action(
            db_session,
            invasion_id,
            stranger,
            expected_group_id=group_id,
            wave_sequence=1,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key="stranger",
            now=at(10),
        )
    await db_session.rollback()


# ----------------------------------------------------------------------
# Enemy strength and balance
# ----------------------------------------------------------------------
def _cfg(waves: int, seconds: int, base: int, cap: int) -> InvasionBattleConfig:
    return InvasionBattleConfig(
        interval_seconds=4 * 60 * 60,
        wave_count=waves,
        wave_seconds=seconds,
        enemy_base_power=base,
        enemy_activity_cap=cap,
        max_compensation_seconds=1200,
    )


@pytest.mark.asyncio
async def test_enemy_power_scales_with_wave_count_and_capped_activity(db_session):
    group = await create_test_group(db_session, "Balance quiet group")
    gid = int(group.id)
    await db_session.commit()
    await MaintenanceService.run_invasion_cycle(now=at(0))
    quiet = await _group_invasion(db_session, gid)
    assert quiet.enemy_power == 200  # (100 + 0) * 4 / 2
    compositions = [json.loads(w.enemy_composition_json)["enemy_power"] for w in await _waves(db_session, quiet.id)]
    assert sum(compositions) == 200
    assert set(compositions) == {50}

    assert MaintenanceService._battle_enemy_power(_cfg(4, 300, 100, 100), 100) == 400
    assert MaintenanceService._battle_enemy_power(_cfg(4, 300, 100, 100), 10_000) == 400


def test_resolution_thresholds_are_unchanged_by_the_four_wave_redesign():
    assert outcome_for(100, 100) == InvasionOutcome.TOTAL_VICTORY
    assert outcome_for(75, 100) == InvasionOutcome.NARROW_DEFENSE
    assert outcome_for(74, 100) == InvasionOutcome.DEFEAT
    assert outcome_for(0, 200) == InvasionOutcome.DEFEAT


@pytest.mark.parametrize(
    ("defense", "enemy", "expected"),
    [
        (200, 200, InvasionOutcome.TOTAL_VICTORY),   # exactly enemy power
        (199, 200, InvasionOutcome.NARROW_DEFENSE),  # just below total victory
        (150, 200, InvasionOutcome.NARROW_DEFENSE),  # exactly 75%
        (149, 200, InvasionOutcome.DEFEAT),          # just below 75%
        (300, 300, InvasionOutcome.TOTAL_VICTORY),   # 50-match enemy power
        (400, 400, InvasionOutcome.TOTAL_VICTORY),   # activity-capped enemy power
        (300, 400, InvasionOutcome.NARROW_DEFENSE),
        (299, 400, InvasionOutcome.DEFEAT),
    ],
)
def test_representative_combat_outcomes_match_the_approved_rule(defense, enemy, expected):
    assert outcome_for(defense, enemy) == expected


def test_enemy_power_formula_is_unchanged_for_representative_match_counts():
    four = _cfg(4, 300, 100, 100)
    two = _cfg(2, 300, 100, 100)
    # Four-wave: (100 + min(matches, 100)) * 4 / 2
    assert MaintenanceService._battle_enemy_power(four, 0) == 200
    assert MaintenanceService._battle_enemy_power(four, 50) == 300
    assert MaintenanceService._battle_enemy_power(four, 100) == 400
    assert MaintenanceService._battle_enemy_power(four, 500) == 400
    # Legacy two-wave equivalent: the same per-wave strength (100 + min(matches, 100)).
    assert MaintenanceService._battle_enemy_power(two, 0) == 100
    assert MaintenanceService._battle_enemy_power(two, 50) == 150
    assert MaintenanceService._battle_enemy_power(two, 100) == 200


@pytest.mark.asyncio
async def test_configured_wave_count_and_duration_are_snapshotted_per_battle(db_session):
    await ConfigService.set(db_session, "invasion_wave_count", 3)
    await ConfigService.set(db_session, "invasion_wave_duration_seconds", 120)
    await db_session.commit()
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))

    waves = await _waves(db_session, invasion_id)
    assert len(waves) == 3
    assert _utc(waves[0].deadline_at) == at(120)

    # Changing configuration later never rewrites a battle already in progress.
    await ConfigService.set(db_session, "invasion_wave_count", 4)
    await ConfigService.set(db_session, "invasion_wave_duration_seconds", 300)
    await db_session.commit()
    await _live(15, 120)
    assert len(await _waves(db_session, invasion_id)) == 3
    await _live(135, 360)
    final = await _load(db_session, invasion_id)
    assert final.status == InvasionStatus.FINISHED
    assert _utc(final.ended_at) == at(360)


# ----------------------------------------------------------------------
# Outage recovery
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_outage_during_a_wave_restores_the_remaining_playable_time(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 60)  # last heartbeat at 60 s: 240 s of wave 1 remain
    restored = await RecoveryService.recover_on_startup(db_session, now=at(1060))
    assert restored["invasion_heartbeat_restores"] == 1
    waves = await _waves(db_session, invasion_id)
    # 1000 s of downtime minus the 30 s tolerance: 300 + 970 = 1270.
    assert _utc(waves[0].deadline_at) == at(1270)
    assert waves[0].status == InvasionStatus.ACTIVE
    assert (_utc(waves[0].deadline_at) - at(1060)).total_seconds() == 210


@pytest.mark.asyncio
@pytest.mark.parametrize("wave_number", [1, 2, 3, 4])
async def test_restart_during_every_wave_restores_that_wave_only(db_session, wave_number):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    wave_start = (wave_number - 1) * WAVE
    # Live until 60 s into the wave, then the bot is down for 1000 s.
    await _live(15, wave_start + 60)
    stats = await RecoveryService.recover_on_startup(db_session, now=at(wave_start + 1060))
    assert stats["invasion_heartbeat_restores"] == 1
    waves = await _waves(db_session, invasion_id)
    # Only the active wave is restored: its deadline moves by 970 s (1000 - 30).
    assert waves[wave_number - 1].status == InvasionStatus.ACTIVE
    assert _utc(waves[wave_number - 1].deadline_at) == at(wave_number * WAVE + 970)
    assert (_utc(waves[wave_number - 1].deadline_at) - at(wave_start + 1060)).total_seconds() == 210
    # Earlier waves stay exactly as they were played.
    for earlier in waves[: wave_number - 1]:
        assert earlier.status == InvasionStatus.FINISHED
        assert _utc(earlier.ended_at) == at(earlier.wave_sequence * WAVE)


@pytest.mark.asyncio
async def test_repeated_recovery_at_the_same_instant_does_not_extend_again(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 60)
    await RecoveryService.recover_on_startup(db_session, now=at(1060))
    first = _utc((await _waves(db_session, invasion_id))[0].deadline_at)
    again = await RecoveryService.recover_on_startup(db_session, now=at(1060))
    assert again["invasion_heartbeat_restores"] == 0
    assert _utc((await _waves(db_session, invasion_id))[0].deadline_at) == first


@pytest.mark.asyncio
async def test_completed_waves_are_not_reopened_by_an_outage(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 300)  # wave 1 ends legitimately at 300; wave 2 starts there
    await RecoveryService.recover_on_startup(db_session, now=at(905))
    waves = await _waves(db_session, invasion_id)
    assert waves[0].status == InvasionStatus.FINISHED
    assert _utc(waves[0].ended_at) == at(300)
    assert waves[1].status == InvasionStatus.ACTIVE
    # Wave 2 had 300 s at its heartbeat (300 s): 600 + (905 - 300 - 30) = 1175.
    assert _utc(waves[1].deadline_at) == at(1175)
    assert (_utc(waves[1].deadline_at) - at(905)).total_seconds() == 270


@pytest.mark.asyncio
async def test_outage_past_the_original_end_within_budget_resumes_instead_of_resolving(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(10, 1100, step=10)  # wave 4 has exactly 100 s left at 1100
    await RecoveryService.recover_on_startup(db_session, now=at(2100))
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.ACTIVE
    waves = await _waves(db_session, invasion_id)
    # 1000 s gap - 30 s tolerance = 970 s compensation, within the 1200 s budget:
    # the 100 s that were left at the heartbeat, less the 30 s tolerance, now run
    # from 2100 to 2170 (wave 4 deadline 1200 + 970).
    assert _utc(waves[3].deadline_at) == at(2170)
    assert (_utc(waves[3].deadline_at) - at(2100)).total_seconds() == 70


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recover_at", "expected_deadline", "expected_status"),
    [
        # Heartbeat at 60 s. Compensation = gap - 30 s tolerance, budget 1200 s.
        (1288, 1498, InvasionStatus.ACTIVE),     # 1198 s: below the budget
        (1289, 1499, InvasionStatus.ACTIVE),     # 1199 s: immediately below
        (1290, 1500, InvasionStatus.ACTIVE),     # 1200 s: exactly at the budget
        (1291, None, InvasionStatus.FINISHED),   # 1201 s: immediately above
    ],
)
async def test_recovery_budget_boundary_below_at_and_above(
    db_session, recover_at, expected_deadline, expected_status
):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 60)  # last heartbeat at 60 s
    stats = await RecoveryService.recover_on_startup(db_session, now=at(recover_at))
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == expected_status
    if expected_status == InvasionStatus.ACTIVE:
        assert stats["invasion_heartbeat_restores"] == 1
        waves = await _waves(db_session, invasion_id)
        assert _utc(waves[0].deadline_at) == at(expected_deadline)
        assert waves[0].status == InvasionStatus.ACTIVE
    else:
        assert stats["invasions_interrupted"] == 1
        assert invasion.outcome is None
        assert _utc(invasion.ended_at) == at(60)


@pytest.mark.asyncio
async def test_budget_is_snapshotted_per_battle_at_activation(db_session):
    await ConfigService.set(db_session, "invasion_downtime_compensation_max_seconds", 60)
    await db_session.commit()
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    # A later configuration change must not rewrite the battle already running.
    await ConfigService.set(db_session, "invasion_downtime_compensation_max_seconds", 1200)
    await db_session.commit()
    await _live(15, 60)
    # 91 s gap - 30 s tolerance = 61 s, one second above the 60 s snapshot.
    stats = await RecoveryService.recover_on_startup(db_session, now=at(151))
    assert stats["invasions_interrupted"] == 1
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome is None


@pytest.mark.asyncio
async def test_zero_budget_interrupts_any_stall_beyond_the_tolerance(db_session):
    await ConfigService.set(db_session, "invasion_downtime_compensation_max_seconds", 0)
    await db_session.commit()
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 60)
    # Heartbeat at 60 s. A 30 s gap is still jitter (no compensation needed),
    # so the battle keeps running and its heartbeat moves to 90 s.
    jitter = await RecoveryService.recover_on_startup(db_session, now=at(90))
    assert jitter.get("invasions_interrupted", 0) == 0
    assert (await _load(db_session, invasion_id)).status == InvasionStatus.ACTIVE
    # From the new heartbeat at 90 s, a 31 s gap is 1 s over tolerance: over budget.
    stats = await RecoveryService.recover_on_startup(db_session, now=at(121))
    assert stats["invasions_interrupted"] == 1
    assert (await _load(db_session, invasion_id)).status == InvasionStatus.FINISHED


@pytest.mark.asyncio
async def test_compensation_budget_is_cumulative_across_outages(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 60)
    # First outage: 700 s gap -> 670 s compensation (within budget).
    await RecoveryService.recover_on_startup(db_session, now=at(760))
    assert _utc((await _waves(db_session, invasion_id))[0].deadline_at) == at(970)
    # Second outage: another 670 s would make 1340 s in total, above 1200 s.
    stats = await RecoveryService.recover_on_startup(db_session, now=at(1460))
    assert stats["invasions_interrupted"] == 1
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome is None
    assert _utc(invasion.ended_at) == at(760)  # last heartbeat before the stall


@pytest.mark.asyncio
async def test_over_budget_interruption_is_terminal_and_not_repeated(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    defender = await _user_id(db_session, "Defender before a terminal outage")
    await InvasionService.join(
        db_session,
        invasion_id,
        defender,
        expected_group_id=group_id,
        now=at(1),
    )
    await InvasionService.perform_defense_action(
        db_session,
        invasion_id,
        defender,
        expected_group_id=group_id,
        wave_sequence=1,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key="terminal-outage:wave-1",
        now=at(15),
    )
    await db_session.commit()
    await _live(30, 60)
    await RecoveryService.recover_on_startup(db_session, now=at(1400))
    first = await _load(db_session, invasion_id)
    first_end = _utc(first.ended_at)
    # The same instant again, a later maintenance tick, and a restart: no change.
    again = await RecoveryService.recover_on_startup(db_session, now=at(1400))
    later = await _live(1415, 1800)
    assert again.get("invasions_interrupted", 0) == 0
    assert later["invasions_resolved"] == 0
    assert later["invasion_wave_transitions"] == 0
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome is None
    assert _utc(invasion.ended_at) == first_end == at(60)
    for wave in await _waves(db_session, invasion_id):
        assert wave.status == InvasionStatus.FINISHED
        assert wave.next_transition_at is None
    # This participant had a real successful Brace before the interruption,
    # but outcome=NULL still means no inventory reward may be granted.
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(UserItem.id)).where(
            UserItem.user_id == defender,
            UserItem.item_key == GOLDEN_APPLE_ITEM_KEY,
        )
    ) == 0


@pytest.mark.asyncio
async def test_join_is_refused_after_an_over_budget_interruption(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    await _live(15, 60)
    late = await _user_id(db_session, "Arrives after the stall")
    with pytest.raises(InvasionJoinClosedError):
        await InvasionService.join(
            db_session, invasion_id, late, expected_group_id=group_id, now=at(1400)
        )
    # A refused join is rolled back with its own transaction, so its interruption
    # is not kept there. The next maintenance tick derives the same terminal
    # state from the persisted heartbeat, so the outcome is durable.
    db_session.expire_all()
    await db_session.rollback()
    stats = await RecoveryService.recover_on_startup(db_session, now=at(1400))
    assert stats["invasions_interrupted"] == 1
    invasion = await _load(db_session, invasion_id)
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome is None


# ----------------------------------------------------------------------
# Telegram presentation: one message per real change
# ----------------------------------------------------------------------
def _telegram_bot() -> MagicMock:
    bot = MagicMock()
    counter = {"id": 9000}

    def next_message(*args, **kwargs):
        counter["id"] += 1
        return SimpleNamespace(message_id=counter["id"])

    bot.send_photo = AsyncMock(side_effect=next_message)
    bot.send_message = AsyncMock(side_effect=next_message)
    bot.edit_message_media = AsyncMock(return_value=True)
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.pin_chat_message = AsyncMock(return_value=True)
    bot.unpin_chat_message = AsyncMock(return_value=True)
    return bot


@pytest.mark.asyncio
async def test_battle_start_is_announced_immediately_pinned_and_recorded(db_session):
    group_id = await _group_id(db_session)
    presentation_ids: list[int] = []
    await MaintenanceService.run_invasion_cycle(now=at(0), presentation_ids=presentation_ids)
    bot = _telegram_bot()
    published = await InvasionTelegramService.publish_maintenance_updates(bot, presentation_ids)
    assert published == 1
    bot.send_photo.assert_awaited_once()
    caption = bot.send_photo.call_args.kwargs["caption"]
    assert "تهاجم آغاز شده است" in caption
    assert "موج ۱ از ۴" in caption
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.call_args.kwargs.get("disable_notification") is True
    invasion = await _group_invasion(db_session, group_id)
    assert invasion.presentation_stage == 1
    assert invasion.announcement_message_id is not None


@pytest.mark.asyncio
async def test_each_wave_start_posts_one_silent_notice_and_no_tick_spam(db_session):
    group_id = await _group_id(db_session)
    await MaintenanceService.run_invasion_cycle(now=at(0))
    bot = _telegram_bot()
    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert bot.send_message.await_count == 0

    # Ordinary ticks within wave 1 publish nothing new.
    for second in range(15, 300, 15):
        await MaintenanceService.run_invasion_cycle(now=at(second), schedule_occurrences=False)
        await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert bot.send_message.await_count == 0

    await MaintenanceService.run_invasion_cycle(now=at(300), schedule_occurrences=False)
    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert bot.send_message.await_count == 1
    notice = bot.send_message.call_args.args[1]
    assert "موج ۲ از ۴ آغاز شد" in notice
    assert bot.send_message.call_args.kwargs.get("disable_notification") is True

    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert bot.send_message.await_count == 1
    invasion = await _group_invasion(db_session, group_id)
    assert invasion.presentation_stage == 2


@pytest.mark.asyncio
async def test_final_result_is_announced_once_with_reward_state(db_session):
    _gid, invasion_id, _d = await _defended_battle(db_session)
    bot = _telegram_bot()
    await InvasionTelegramService.publish_maintenance_updates(bot, [invasion_id])  # start
    assert bot.send_photo.await_count == 1

    await _live(15, 1200)
    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    results = [c for c in bot.send_message.call_args_list if "یورش به پایان رسید" in c.args[1]]
    assert len(results) == 1
    assert "🎁" in results[0].args[1]
    # The reward is named accurately: Golden Apple inventory, never Apple wallet.
    assert "🌕 سیب طلایی" in results[0].args[1]
    assert "🍎" not in results[0].args[1]
    assert results[0].kwargs.get("disable_notification") is not True

    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert len([c for c in bot.send_message.call_args_list if "یورش به پایان رسید" in c.args[1]]) == 1


@pytest.mark.asyncio
async def test_interrupted_battle_gets_one_notice_that_is_not_a_defeat(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    bot = _telegram_bot()
    await InvasionTelegramService.publish_maintenance_updates(bot, [invasion_id])
    await _live(15, 60)
    await RecoveryService.recover_on_startup(db_session, now=at(1400))  # over budget
    await InvasionTelegramService.publish_maintenance_updates(bot, [invasion_id])
    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    notices = [
        c.args[1]
        for c in bot.send_message.call_args_list
        if "یورش به پایان رسید" in c.args[1]
    ]
    assert len(notices) == 1
    assert "بدون نتیجه" in notices[0]
    assert "🏆 نتیجه" not in notices[0]  # no ordinary victory/defeat line
    assert "پاداشی تعلق نمی‌گیرد" in notices[0]


@pytest.mark.asyncio
async def test_failed_notice_is_retried_and_recovery_never_resends_the_start(db_session):
    group_id = await _group_id(db_session)
    await MaintenanceService.run_invasion_cycle(now=at(0))
    bot = _telegram_bot()
    await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert bot.send_photo.await_count == 1

    # A restart after startup recovery must not send the battle a second time.
    await RecoveryService.recover_on_startup(db_session, now=at(30))
    restart_bot = _telegram_bot()
    await InvasionTelegramService.publish_maintenance_updates(restart_bot, [])
    assert restart_bot.send_photo.await_count == 0

    await _live(45, 300)  # wave 2 begins at 300; the stage notice is due
    failing = _telegram_bot()
    failing.send_message = AsyncMock(return_value=None)
    await InvasionTelegramService.publish_maintenance_updates(failing, [])
    invasion = await _group_invasion(db_session, group_id)
    assert invasion.presentation_stage == 1  # the failed notice was not recorded

    invasion_telegram._STAGE_NOTICE_RETRY_AFTER.clear()
    recovered = _telegram_bot()
    await InvasionTelegramService.publish_maintenance_updates(recovered, [])
    assert recovered.send_message.await_count == 1
    invasion = await _group_invasion(db_session, group_id)
    assert invasion.presentation_stage == 2


@pytest.mark.asyncio
async def test_wave_notice_text_comes_from_persisted_state(db_session):
    group_id = await _group_id(db_session)
    invasion_id = await _activate(db_session, group_id, at(0))
    snapshot = await InvasionTelegramService.load_snapshot(invasion_id)
    text = render_wave_notice(snapshot, now=at(0))
    assert "موج ۱ از ۴ آغاز شد" in text
    assert "۵ دقیقه" in text
    assert "🛡 دفاع این موج: ۰" in text


# ----------------------------------------------------------------------
# Incident regression: wave-1 Brace, then a prolonged outage, closes the
# battle as interrupted (no outcome, no rewards, preserved region name).
# ----------------------------------------------------------------------
async def _seed_moon_descendants_faction(session) -> None:
    """Ensure the Moon Children faction exists with its territory lore.

    Production always seeds factions on startup; the isolated test database
    only runs ``seed_default_data`` (games/channels/ads), so tests that
    touch faction/region metadata seed the row explicitly.
    """
    from sib.database.models import Faction as _Faction
    existing = await session.scalar(
        select(_Faction.id).where(_Faction.code == "moon_descendants")
    )
    if existing is not None:
        return
    session.add(
        _Faction(
            code="moon_descendants",
            name_fa="فرزندان ماه",
            name_en="Moon Descendants",
            symbol="🌙",
            lore_fa="ساکنان درّه ماه.",
            territory_fa="درّه ماه (Moonfall Valley)",
            display_order=3,
            is_active=True,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_incident_wave_one_brace_then_long_outage_is_interrupted_without_rewards(db_session):
    """Reproduce the 2026-10-11 incident timeline end-to-end.

    The player joins, performs one Brace on wave 1, the bot stays down
    past the downtime compensation budget, startup recovery fires, and
    the battle must terminate FINISHED with outcome NULL, zero rewards
    granted, exactly one participant and one defense action preserved,
    the target-region snapshot populated by the scheduler, and the final
    Telegram notice describing a closed-without-result outcome.
    """
    await _seed_moon_descendants_faction(db_session)
    group_id = await _group_id(db_session)
    defender = await _user_id(db_session, "Incident defender")

    # 1) Activate via the real scheduler path so region metadata is set.
    await MaintenanceService.run_invasion_cycle(now=at(0))
    db_session.expire_all()
    invasion = await _group_invasion(db_session, group_id)
    assert invasion is not None
    invasion_id = int(invasion.id)
    assert invasion.status == InvasionStatus.ACTIVE
    assert invasion.enemy_power > 0
    # The scheduler fix must have populated the region display name.
    assert invasion.target_region_name_snapshot == "درّه ماه (Moonfall Valley)"

    # 2) Player joins and performs one Brace in wave 1 at t=15.
    await InvasionService.join(
        db_session, invasion_id, defender, expected_group_id=group_id, now=at(1)
    )
    await InvasionService.perform_defense_action(
        db_session,
        invasion_id,
        defender,
        expected_group_id=group_id,
        wave_sequence=1,
        action_code=InvasionDefenseActionCode.BRACE,
        idempotency_key="incident:wave-1:brace",
        now=at(15),
    )
    await db_session.commit()

    # 3) Simulate live ticks only up to t=60 (a 45-second cadence), then
    #    bot outage. No maintenance runs until recovery at t=1400 s, which
    #    is past the default 20-minute compensation budget (1200 s) plus
    #    the 30 s heartbeat tolerance.
    await _live(30, 60)
    await RecoveryService.recover_on_startup(db_session, now=at(1400))

    # 4) Final persisted state must match the incident observations.
    db_session.expire_all()
    invasion = await db_session.get(Invasion, invasion_id)
    assert invasion is not None
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome is None  # no win/loss recorded
    assert invasion.ended_at is not None
    assert invasion.started_at is not None
    assert invasion.rewards_settled_at is not None  # stamped: nothing owed
    assert invasion.current_wave_sequence is None
    assert invasion.next_transition_at is None
    # Waves are all closed.
    waves = list(
        (
            await db_session.scalars(
                select(InvasionWave)
                .where(InvasionWave.invasion_id == invasion_id)
                .order_by(InvasionWave.wave_sequence.asc())
            )
        ).all()
    )
    assert len(waves) == 4
    assert all(w.status == InvasionStatus.FINISHED for w in waves)
    assert all(w.next_transition_at is None for w in waves)

    # Participant / action counts preserved.
    participant_count = await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id
        )
    )
    action_count = await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    )
    assert participant_count == 1
    assert action_count == 1
    # Wave 1 holds the Brace contribution; later waves hold 0.
    assert waves[0].contribution_total > 0
    assert sum(w.contribution_total for w in waves[1:]) == 0

    # No rewards granted (outcome NULL excludes the battle from settlement).
    assert (
        await db_session.scalar(
            select(func.count(UserItem.id)).where(
                UserItem.user_id == defender,
                UserItem.item_key == GOLDEN_APPLE_ITEM_KEY,
            )
        )
    ) == 0

    # 5) Final Telegram notice describes the outage closure, not a defeat.
    snapshot = await InvasionTelegramService.load_snapshot(invasion_id)
    assert snapshot is not None
    final_text = render_final_notice(snapshot)
    status_text = render_invasion_status(snapshot, now=at(1400))
    assert "بدون نتیجه" in final_text
    assert "پاداشی تعلق نمی‌گیرد" in final_text
    assert "قطع طولانی ربات" in status_text
    # The region must still be shown (not "مشخص نشده").
    assert "درّه ماه" in final_text
    assert "مشخص نشده" not in final_text

    # 6) Idempotency: re-running recovery/maintenance changes nothing.
    again = await RecoveryService.recover_on_startup(db_session, now=at(1400))
    assert again.get("invasions_interrupted", 0) == 0
    assert again.get("invasions_resolved", 0) == 0
    db_session.expire_all()
    invasion2 = await db_session.get(Invasion, invasion_id)
    assert invasion2.status == InvasionStatus.FINISHED
    assert invasion2.outcome is None
    assert invasion2.ended_at == invasion.ended_at
