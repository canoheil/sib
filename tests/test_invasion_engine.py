"""Domain-engine tests for persistent Invasion lifecycle and aggregates."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    Invasion,
    InvasionContribution,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
)
from sib.services.card_progression import card_attributes
from sib.services.characters import CharacterService
from sib.services.invasion import (
    MAX_SIGNED_BIGINT,
    ContributionResult,
    InvasionAlreadyResolvedError,
    InvasionConfigurationError,
    InvasionContributionConflictError,
    InvasionContributionError,
    InvasionGroupMismatchError,
    InvasionGroupNotFoundError,
    InvasionJoinClosedError,
    InvasionOccurrenceConflictError,
    InvasionParticipantMismatchError,
    InvasionParticipantNotFoundError,
    InvasionResolutionResult,
    InvasionService,
    InvasionStateError,
    InvasionTransitionNotDueError,
    InvasionWaveError,
)
from sib.services.player_cards import PlayerCardService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_group, create_test_user


UTC = timezone.utc
NOW = datetime(2026, 10, 7, 12, 30, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _wave_composition(sequence: int) -> dict:
    return {
        "enemies": [{"code": f"enemy_{sequence}", "count": sequence}],
        "power": 10 * sequence,
    }


async def _create_invasion(
    session,
    group,
    *,
    occurrence_key=None,
    scheduled_at=None,
    enemy_power=100,
    wave_count=1,
    event_config=None,
    event_spec=None,
):
    return await InvasionService.create(
        session,
        group_id=group.id,
        occurrence_key=occurrence_key or f"invasion-{uuid.uuid4().hex}",
        scheduled_at=scheduled_at or NOW - timedelta(minutes=1),
        enemy_faction_code="moon_descendants",
        enemy_power=enemy_power,
        event_config=event_config or {"difficulty": "test", "version": 1},
        event_spec=event_spec,
        wave_compositions=[
            _wave_composition(sequence) for sequence in range(1, wave_count + 1)
        ],
    )


async def _start_invasion(session, group, **kwargs):
    invasion = await _create_invasion(session, group, **kwargs)
    await InvasionService.start(session, invasion.id, now=NOW)
    return invasion


async def _join(session, invasion, user):
    return await InvasionService.join(
        session,
        invasion.id,
        user.id,
        expected_group_id=invasion.group_id,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_create_persists_event_and_wave_configuration_and_is_idempotent(db_session):
    group = await create_test_group(db_session, "Invasion create group")
    occurrence_key = "engine-create-occurrence"
    scheduled_at = NOW + timedelta(hours=1)
    event_config = {"difficulty": {"tier": 2, "modifiers": ["fixed"]}}
    event_spec = {"seed": "stable-seed", "ruleset": {"revision": 3}}

    invasion = await _create_invasion(
        db_session,
        group,
        occurrence_key=occurrence_key,
        scheduled_at=scheduled_at,
        enemy_power=420,
        wave_count=3,
        event_config=event_config,
        event_spec=event_spec,
    )
    await db_session.commit()

    assert invasion.status == InvasionStatus.SCHEDULED
    assert invasion.started_at is None
    assert invasion.ended_at is None
    assert invasion.outcome is None
    assert invasion.current_wave_sequence == 1
    assert invasion.next_transition_at.replace(tzinfo=UTC) == scheduled_at
    assert json.loads(invasion.event_config_json) == event_config
    persisted_spec = json.loads(invasion.event_spec_json)
    assert persisted_spec == {
        "waves": [_wave_composition(1), _wave_composition(2), _wave_composition(3)],
        "metadata": event_spec,
    }
    waves = list(
        (
            await db_session.scalars(
                select(InvasionWave)
                .where(InvasionWave.invasion_id == invasion.id)
                .order_by(InvasionWave.wave_sequence)
            )
        ).all()
    )
    assert [wave.wave_sequence for wave in waves] == [1, 2, 3]
    assert [json.loads(wave.enemy_composition_json) for wave in waves] == persisted_spec[
        "waves"
    ]
    assert [wave.status for wave in waves] == [
        InvasionStatus.SCHEDULED,
        InvasionStatus.SCHEDULED,
        InvasionStatus.SCHEDULED,
    ]
    assert waves[0].next_transition_at.replace(tzinfo=UTC) == scheduled_at
    assert waves[1].next_transition_at is None

    replay = await _create_invasion(
        db_session,
        group,
        occurrence_key=occurrence_key,
        scheduled_at=scheduled_at,
        enemy_power=420,
        wave_count=3,
        event_config=event_config,
        event_spec=event_spec,
    )
    assert replay.id == invasion.id
    assert await db_session.scalar(
        select(func.count(Invasion.id)).where(Invasion.occurrence_key == occurrence_key)
    ) == 1

    with pytest.raises(InvasionOccurrenceConflictError):
        await _create_invasion(
            db_session,
            group,
            occurrence_key=occurrence_key,
            scheduled_at=scheduled_at,
            enemy_power=421,
            wave_count=3,
            event_config=event_config,
            event_spec=event_spec,
        )


@pytest.mark.asyncio
async def test_create_rejects_missing_or_non_json_wave_configuration(db_session):
    group = await create_test_group(db_session, "Invalid Invasion config")
    base = {
        "group_id": group.id,
        "occurrence_key": "invalid-config",
        "scheduled_at": NOW,
        "enemy_faction_code": "moon_descendants",
        "enemy_power": 100,
        "event_config": {},
    }
    with pytest.raises(InvasionConfigurationError):
        await InvasionService.create(db_session, **base, wave_compositions=[])
    with pytest.raises(InvasionConfigurationError):
        await InvasionService.create(db_session, **base, wave_compositions=["not-an-object"])


@pytest.mark.asyncio
async def test_start_enforces_scheduled_time_and_is_idempotent(db_session):
    group = await create_test_group(db_session, "Invasion lifecycle group")
    scheduled_at = NOW + timedelta(minutes=5)
    invasion = await _create_invasion(
        db_session, group, scheduled_at=scheduled_at, wave_count=2
    )

    with pytest.raises(InvasionTransitionNotDueError):
        await InvasionService.start(session=db_session, invasion_id=invasion.id, now=NOW)
    assert invasion.status == InvasionStatus.SCHEDULED

    deadline = scheduled_at + timedelta(minutes=10)
    started = await InvasionService.start(
        db_session,
        invasion.id,
        now=scheduled_at,
        deadline_at=deadline,
    )
    original_started_at = started.started_at
    first_wave = await InvasionService.get_current_wave(db_session, invasion.id)
    assert started.status == InvasionStatus.ACTIVE
    assert started.started_at.replace(tzinfo=UTC) == scheduled_at
    assert first_wave.status == InvasionStatus.ACTIVE
    assert first_wave.deadline_at.replace(tzinfo=UTC) == deadline

    replay = await InvasionService.start(
        db_session,
        invasion.id,
        now=scheduled_at + timedelta(hours=1),
        deadline_at=deadline,
    )
    assert replay.id == invasion.id
    assert _utc(replay.started_at) == _utc(original_started_at)
    assert first_wave.status == InvasionStatus.ACTIVE

    with pytest.raises(InvasionWaveError):
        await InvasionService.start(
            db_session,
            invasion.id,
            now=scheduled_at + timedelta(hours=1),
            deadline_at=deadline + timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_creation_and_join_stay_inside_the_callers_transaction(db_session):
    group = await create_test_group(db_session, "Invasion caller transaction group")
    user = await create_test_user(db_session, "Invasion caller transaction user")
    invasion = await _create_invasion(db_session, group)
    await InvasionService.start(db_session, invasion.id, now=NOW)
    participant = await _join(db_session, invasion, user)
    invasion_id = invasion.id

    assert await db_session.scalar(
        select(func.count(Invasion.id)).where(Invasion.id == invasion_id)
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id
        )
    ) == 1
    await db_session.rollback()

    assert await db_session.scalar(
        select(func.count(Invasion.id)).where(Invasion.id == invasion_id)
    ) == 0
    assert await db_session.scalar(
        select(func.count(InvasionWave.id)).where(
            InvasionWave.invasion_id == invasion_id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id
        )
    ) == 0
    assert participant.id is not None


@pytest.mark.asyncio
async def test_invalid_prestart_operations_are_rejected_deterministically(db_session):
    group = await create_test_group(db_session, "Invasion prestart group")
    user = await create_test_user(db_session, "Invasion prestart user")
    invasion = await _create_invasion(db_session, group)

    with pytest.raises(InvasionJoinClosedError):
        await _join(db_session, invasion, user)
    with pytest.raises(InvasionStateError):
        await InvasionService.resolve(db_session, invasion.id, now=NOW)
    with pytest.raises(InvasionStateError):
        await InvasionService.finish_wave(db_session, invasion.id, 1, now=NOW)

    assert invasion.status == InvasionStatus.SCHEDULED
    assert await InvasionService.has_remaining_waves(db_session, invasion.id) is True


@pytest.mark.asyncio
async def test_join_checks_group_and_duplicate_join_keeps_one_snapshot(db_session):
    group = await create_test_group(db_session, "Invasion join group")
    other_group = await create_test_group(db_session, "Other Invasion group")
    user = await create_test_user(db_session, "Main Card joiner")
    character = await CharacterService.create(
        db_session,
        code=f"invasion_join_{uuid.uuid4().hex[:8]}",
        name_fa="قهرمان یورش",
        name_en="Invasion Hero",
        artwork="hero-before.png",
        power=20,
        speed=20,
        intelligence=20,
    )
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    card.level = 3
    await db_session.flush()
    await PlayerCardService.set_favorite(
        db_session, user.id, card.id, expected_main_card_id=0
    )
    await db_session.commit()

    invasion = await _start_invasion(db_session, group)
    with pytest.raises(InvasionGroupMismatchError):
        await InvasionService.join(
            db_session,
            invasion.id,
            user.id,
            expected_group_id=other_group.id,
            now=NOW,
        )

    participant = await _join(db_session, invasion, user)
    expected_stats = card_attributes(card, character)
    assert participant.main_card_id_snapshot == card.id
    assert participant.character_id_snapshot == character.id
    assert participant.character_code_snapshot == character.code
    assert participant.character_name_fa_snapshot == character.name_fa
    assert participant.character_name_en_snapshot == character.name_en
    assert participant.character_artwork_snapshot == character.artwork
    assert participant.card_level_snapshot == 3
    assert participant.power_snapshot == expected_stats.power
    assert participant.speed_snapshot == expected_stats.speed
    assert participant.intelligence_snapshot == expected_stats.intelligence
    assert (
        participant.character_balance_version_snapshot == character.balance_version
    )

    # Switching the live Main Card and changing Character balance cannot rewrite
    # the historical snapshot. A retry returns the original participant too.
    other_character = await CharacterService.create(
        db_session,
        code=f"invasion_other_{uuid.uuid4().hex[:8]}",
        name_fa="قهرمان دوم",
        power=20,
        speed=20,
        intelligence=20,
    )
    other_card = await PlayerCardService.acquire(
        db_session, user.id, other_character.id
    )
    await PlayerCardService.set_favorite(
        db_session,
        user.id,
        other_card.id,
        expected_main_card_id=card.id,
    )
    await CharacterService.update(
        db_session,
        character.id,
        power=30,
        speed=20,
        intelligence=10,
        note="snapshot test rebalance",
    )
    await db_session.commit()

    retry = await _join(db_session, invasion, user)
    assert retry.id == participant.id
    assert retry.main_card_id_snapshot == card.id
    assert retry.card_level_snapshot == 3
    assert retry.power_snapshot == expected_stats.power
    assert retry.character_balance_version_snapshot == 1
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion.id,
            InvasionParticipant.user_id == user.id,
        )
    ) == 1


@pytest.mark.asyncio
async def test_no_main_card_is_not_auto_selected_and_participant_can_contribute(db_session):
    group = await create_test_group(db_session, "Invasion no Main Card group")
    user = await create_test_user(db_session, "No Main Card participant")
    character = await CharacterService.create(
        db_session,
        code=f"invasion_unselected_{uuid.uuid4().hex[:8]}",
        name_fa="کارت انتخاب‌نشده",
        power=20,
        speed=20,
        intelligence=20,
    )
    unselected_card = await PlayerCardService.acquire(
        db_session, user.id, character.id
    )
    await db_session.commit()
    assert not unselected_card.is_favorite
    assert await PlayerCardService.get_main_card(db_session, user.id) is None

    invasion = await _start_invasion(db_session, group)
    participant = await _join(db_session, invasion, user)
    snapshot_fields = (
        "main_card_id_snapshot",
        "character_id_snapshot",
        "character_code_snapshot",
        "character_name_fa_snapshot",
        "character_name_en_snapshot",
        "character_artwork_snapshot",
        "card_level_snapshot",
        "power_snapshot",
        "speed_snapshot",
        "intelligence_snapshot",
        "character_balance_version_snapshot",
    )
    assert all(getattr(participant, field) is None for field in snapshot_fields)
    assert not unselected_card.is_favorite

    result = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        7,
        idempotency_key="no-main-card:1",
        now=NOW,
    )
    assert isinstance(result, ContributionResult)
    assert result.participant_total == 7
    assert result.wave_total == 7
    assert result.invasion_total == 7


@pytest.mark.asyncio
async def test_wave_transitions_use_persisted_state_and_are_idempotent(db_session):
    group = await create_test_group(db_session, "Invasion wave transitions")
    invasion = await _start_invasion(db_session, group, wave_count=2)
    first = await InvasionService.get_current_wave(db_session, invasion.id)
    assert first.wave_sequence == 1
    assert first.status == InvasionStatus.ACTIVE
    assert json.loads(first.enemy_composition_json) == _wave_composition(1)

    # An active wave without a deadline is returned unchanged on retry.
    repeated = await InvasionService.advance_wave(
        db_session, invasion.id, 1, now=NOW
    )
    assert repeated.id == first.id
    assert first.status == InvasionStatus.ACTIVE

    first_end = NOW + timedelta(minutes=5)
    finished_first = await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=first_end
    )
    assert finished_first.status == InvasionStatus.FINISHED
    assert finished_first.ended_at.replace(tzinfo=UTC) == first_end
    assert invasion.current_wave_sequence == 2
    second = await InvasionService.get_current_wave(db_session, invasion.id)
    assert second.status == InvasionStatus.SCHEDULED
    assert second.next_transition_at.replace(tzinfo=UTC) == first_end
    assert await InvasionService.has_remaining_waves(db_session, invasion.id) is True

    # Replaying the old finish doesn't move the pointer or change its timestamp.
    replayed_finish = await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=first_end + timedelta(minutes=1)
    )
    assert replayed_finish.ended_at.replace(tzinfo=UTC) == first_end
    with pytest.raises(InvasionTransitionNotDueError):
        await InvasionService.advance_wave(
            db_session, invasion.id, 2, now=first_end - timedelta(seconds=1)
        )

    started_second = await InvasionService.advance_wave(
        db_session, invasion.id, 2, now=first_end
    )
    assert started_second.id == second.id
    assert started_second.status == InvasionStatus.ACTIVE
    assert started_second.started_at.replace(tzinfo=UTC) == first_end

    second_end = first_end + timedelta(minutes=5)
    await InvasionService.finish_wave(db_session, invasion.id, 2, now=second_end)
    assert invasion.current_wave_sequence is None
    assert await InvasionService.get_current_wave(db_session, invasion.id) is None
    assert await InvasionService.has_remaining_waves(db_session, invasion.id) is False
    assert (
        await InvasionService.advance_wave(db_session, invasion.id, 2, now=second_end)
        is None
    )


@pytest.mark.asyncio
async def test_persisted_deadline_closes_contributions_and_advances_wave(db_session):
    group = await create_test_group(db_session, "Invasion deadline group")
    user = await create_test_user(db_session, "Deadline participant")
    invasion = await _create_invasion(db_session, group, wave_count=2)
    deadline = NOW + timedelta(minutes=2)
    await InvasionService.start(
        db_session, invasion.id, now=NOW, deadline_at=deadline
    )
    participant = await _join(db_session, invasion, user)

    before_deadline = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        3,
        idempotency_key="deadline:accepted",
        now=deadline - timedelta(microseconds=1),
    )
    assert before_deadline.wave_total == 3
    with pytest.raises(InvasionContributionError, match="deadline"):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            1,
            idempotency_key="deadline:rejected",
            now=deadline,
        )

    next_scheduled = await InvasionService.advance_wave(
        db_session, invasion.id, 1, now=deadline
    )
    assert invasion.current_wave_sequence == 2
    assert next_scheduled.status == InvasionStatus.SCHEDULED
    assert next_scheduled.next_transition_at.replace(tzinfo=UTC) == deadline
    # Retrying the first wave's deadline transition cannot also start wave two.
    replay = await InvasionService.advance_wave(
        db_session, invasion.id, 1, now=deadline
    )
    assert replay.id == next_scheduled.id
    assert replay.status == InvasionStatus.SCHEDULED
    started_next = await InvasionService.advance_wave(
        db_session, invasion.id, 2, now=deadline
    )
    assert started_next.status == InvasionStatus.ACTIVE
    assert started_next.started_at.replace(tzinfo=UTC) == deadline


@pytest.mark.asyncio
async def test_contribution_validates_wave_participant_and_positive_amount(db_session):
    group = await create_test_group(db_session, "Invasion contribution group")
    other_group = await create_test_group(db_session, "Other contribution group")
    user = await create_test_user(db_session, "Contribution participant")
    other_user = await create_test_user(db_session, "Other contribution participant")
    invasion = await _start_invasion(db_session, group, wave_count=2)
    other_invasion = await _start_invasion(db_session, other_group)
    participant = await _join(db_session, invasion, user)
    other_participant = await _join(db_session, other_invasion, other_user)

    with pytest.raises(InvasionContributionError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            1,
            idempotency_key="",
            now=NOW,
        )
    with pytest.raises(InvasionContributionError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            0,
            idempotency_key="invalid:zero",
            now=NOW,
        )
    with pytest.raises(InvasionContributionError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            -1,
            idempotency_key="invalid:negative",
            now=NOW,
        )
    with pytest.raises(InvasionContributionError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            MAX_SIGNED_BIGINT + 1,
            idempotency_key="invalid:overflow",
            now=NOW,
        )
    with pytest.raises(InvasionWaveError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            2,
            1,
            idempotency_key="invalid:wave",
            now=NOW,
        )
    with pytest.raises(InvasionParticipantMismatchError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            other_participant.id,
            1,
            1,
            idempotency_key="invalid:participant",
            now=NOW,
        )
    with pytest.raises(InvasionParticipantNotFoundError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            999999999,
            1,
            1,
            idempotency_key="invalid:missing-participant",
            now=NOW,
        )

    result = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        12,
        idempotency_key="contribution:valid",
        now=NOW,
    )
    assert result.participant_total == 12
    assert result.wave_total == 12
    assert result.invasion_total == 12
    assert participant.contribution_total == 12
    current = await InvasionService.get_current_wave(db_session, invasion.id)
    assert current.contribution_total == 12


@pytest.mark.asyncio
async def test_contribution_key_deduplicates_replays_and_conflicting_payloads(db_session):
    group = await create_test_group(db_session, "Invasion contribution retry group")
    user = await create_test_user(db_session, "Contribution retry participant")
    invasion = await _start_invasion(db_session, group, wave_count=2)
    participant = await _join(db_session, invasion, user)

    first = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        12,
        idempotency_key="request-unique-1",
        now=NOW,
    )
    replay = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        12,
        idempotency_key="request-unique-1",
        now=NOW,
    )
    assert first.duplicate is False
    assert replay.duplicate is True
    assert replay.participant_total == 12
    assert replay.wave_total == 12
    assert replay.invasion_total == 12

    with pytest.raises(InvasionContributionConflictError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            13,
            idempotency_key="request-unique-1",
            now=NOW,
        )

    await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=NOW + timedelta(seconds=1)
    )
    await InvasionService.advance_wave(
        db_session, invasion.id, 2, now=NOW + timedelta(seconds=1)
    )
    # A retry of a completed request is still recognized after its wave moved on.
    stale_replay = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        12,
        idempotency_key="request-unique-1",
        now=NOW + timedelta(seconds=2),
    )
    assert stale_replay.duplicate is True
    assert stale_replay.participant_total == 12
    assert stale_replay.wave_total == 12

    second = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        2,
        5,
        idempotency_key="request-unique-2",
        now=NOW + timedelta(seconds=2),
    )
    assert second.participant_total == 17
    assert second.invasion_total == 17
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion.id
        )
    ) == 2


@pytest.mark.asyncio
async def test_contribution_updates_are_rolled_back_with_callers_transaction(db_session):
    group = await create_test_group(db_session, "Invasion contribution rollback")
    user = await create_test_user(db_session, "Rollback participant")
    invasion = await _start_invasion(db_session, group)
    participant = await _join(db_session, invasion, user)
    invasion_id, participant_id = invasion.id, participant.id
    await db_session.commit()

    result = await InvasionService.record_contribution(
        db_session,
        invasion_id,
        participant_id,
        1,
        20,
        idempotency_key="rollback:20",
        now=NOW,
    )
    assert result.wave_total == 20
    await db_session.rollback()

    persisted_participant = await db_session.get(InvasionParticipant, participant_id)
    persisted_wave = await db_session.scalar(
        select(InvasionWave).where(
            InvasionWave.invasion_id == invasion_id,
            InvasionWave.wave_sequence == 1,
        )
    )
    assert persisted_participant.contribution_total == 0
    assert persisted_wave.contribution_total == 0
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defense", "expected"),
    [
        (100, InvasionOutcome.TOTAL_VICTORY),
        (120, InvasionOutcome.TOTAL_VICTORY),
        (99, InvasionOutcome.NARROW_DEFENSE),
        (75, InvasionOutcome.NARROW_DEFENSE),
        (74, InvasionOutcome.DEFEAT),
    ],
)
async def test_resolution_uses_exact_integer_thresholds_and_is_idempotent(
    db_session, defense, expected
):
    group = await create_test_group(db_session, f"Threshold group {defense}")
    user = await create_test_user(db_session, f"Threshold user {defense}")
    invasion = await _start_invasion(db_session, group, enemy_power=100)
    participant = await _join(db_session, invasion, user)
    if defense:
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            defense,
            idempotency_key=f"threshold:{defense}",
            now=NOW,
        )
    await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=NOW + timedelta(seconds=1)
    )

    result = await InvasionService.resolve(
        db_session, invasion.id, now=NOW + timedelta(seconds=2)
    )
    assert isinstance(result, InvasionResolutionResult)
    assert result.outcome == expected
    assert result.defense_total == defense
    assert result.enemy_power == 100
    assert result.duplicate is False
    assert invasion.status == InvasionStatus.FINISHED
    assert invasion.outcome == expected
    assert invasion.ended_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=2)

    ended_at = _utc(invasion.ended_at)
    replay = await InvasionService.resolve(
        db_session, invasion.id, now=NOW + timedelta(hours=1)
    )
    assert replay.outcome == expected
    assert replay.defense_total == defense
    assert replay.duplicate is True
    assert _utc(invasion.ended_at) == ended_at


@pytest.mark.asyncio
async def test_resolution_aggregates_persisted_contributions_across_waves(db_session):
    group = await create_test_group(db_session, "Invasion aggregate group")
    user = await create_test_user(db_session, "Aggregate participant")
    invasion = await _start_invasion(db_session, group, enemy_power=100, wave_count=2)
    participant = await _join(db_session, invasion, user)

    first = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        30,
        idempotency_key="aggregate:wave-1",
        now=NOW,
    )
    await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=NOW + timedelta(seconds=1)
    )
    await InvasionService.advance_wave(
        db_session, invasion.id, 2, now=NOW + timedelta(seconds=1)
    )
    second = await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        2,
        45,
        idempotency_key="aggregate:wave-2",
        now=NOW + timedelta(seconds=2),
    )
    await InvasionService.finish_wave(
        db_session, invasion.id, 2, now=NOW + timedelta(seconds=3)
    )

    assert first.invasion_total == 30
    assert second.participant_total == 75
    assert second.invasion_total == 75
    assert participant.contribution_total == 75
    result = await InvasionService.resolve(
        db_session, invasion.id, now=NOW + timedelta(seconds=4)
    )
    assert result.defense_total == 75
    assert result.outcome == InvasionOutcome.NARROW_DEFENSE


@pytest.mark.asyncio
async def test_resolution_requires_all_waves_and_finished_event_rejects_mutation(db_session):
    group = await create_test_group(db_session, "Invasion immutable group")
    user = await create_test_user(db_session, "Immutable participant")
    another_user = await create_test_user(db_session, "Late joiner")
    invasion = await _start_invasion(db_session, group)
    participant = await _join(db_session, invasion, user)

    with pytest.raises(InvasionWaveError):
        await InvasionService.resolve(db_session, invasion.id, now=NOW)

    await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        70,
        idempotency_key="immutable:accepted",
        now=NOW,
    )
    await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=NOW + timedelta(seconds=1)
    )
    await InvasionService.resolve(
        db_session, invasion.id, now=NOW + timedelta(seconds=2)
    )
    totals_after_resolution = (
        participant.contribution_total,
        (await InvasionService.get_current_wave(db_session, invasion.id)),
    )

    with pytest.raises(InvasionAlreadyResolvedError):
        await InvasionService.start(db_session, invasion.id, now=NOW + timedelta(hours=1))
    with pytest.raises(InvasionJoinClosedError):
        await _join(db_session, invasion, another_user)
    with pytest.raises(InvasionAlreadyResolvedError):
        await InvasionService.advance_wave(
            db_session, invasion.id, 1, now=NOW + timedelta(hours=1)
        )
    with pytest.raises(InvasionAlreadyResolvedError):
        await InvasionService.finish_wave(db_session, invasion.id, 1, now=NOW + timedelta(hours=1))
    with pytest.raises(InvasionAlreadyResolvedError):
        await InvasionService.record_contribution(
            db_session,
            invasion.id,
            participant.id,
            1,
            1,
            idempotency_key="immutable:late",
            now=NOW + timedelta(hours=1),
        )

    assert totals_after_resolution[0] == participant.contribution_total == 70
    assert totals_after_resolution[1] is None
    assert invasion.status == InvasionStatus.FINISHED


@pytest.mark.asyncio
async def test_join_after_final_resolution_is_closed_even_for_existing_participant(db_session):
    group = await create_test_group(db_session, "Invasion final join group")
    user = await create_test_user(db_session, "Final join participant")
    invasion = await _start_invasion(db_session, group)
    participant = await _join(db_session, invasion, user)
    await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=NOW + timedelta(seconds=1)
    )
    await InvasionService.resolve(db_session, invasion.id, now=NOW + timedelta(seconds=2))

    with pytest.raises(InvasionJoinClosedError):
        await _join(db_session, invasion, user)
    assert participant.id is not None


@pytest.mark.asyncio
async def test_group_must_exist_and_duplicate_key_configuration_is_stable(db_session):
    with pytest.raises(InvasionGroupNotFoundError):
        await InvasionService.create(
            db_session,
            group_id=-999999999,
            occurrence_key="missing-group",
            scheduled_at=NOW,
            enemy_faction_code="moon_descendants",
            enemy_power=100,
            event_config={},
            wave_compositions=[_wave_composition(1)],
        )

    group = await create_test_group(db_session, "Stable occurrence group")
    first = await _create_invasion(
        db_session,
        group,
        occurrence_key="stable-occurrence",
        scheduled_at=NOW,
        event_config={"b": 2, "a": 1},
        event_spec={"seed": 19},
    )
    # Canonical JSON makes mapping insertion order immaterial.
    second = await _create_invasion(
        db_session,
        group,
        occurrence_key="stable-occurrence",
        scheduled_at=NOW,
        event_config={"a": 1, "b": 2},
        event_spec={"seed": 19},
    )
    assert first.id == second.id


@pytest.mark.asyncio
async def test_scheduled_wave_cannot_be_finished_or_started_before_transition(db_session):
    group = await create_test_group(db_session, "Invasion early wave group")
    invasion = await _create_invasion(
        db_session,
        group,
        scheduled_at=NOW + timedelta(minutes=1),
        wave_count=2,
    )
    with pytest.raises(InvasionTransitionNotDueError):
        await InvasionService.start(db_session, invasion.id, now=NOW)
    with pytest.raises(InvasionStateError):
        await InvasionService.advance_wave(db_session, invasion.id, 1, now=NOW)

    assert invasion.status == InvasionStatus.SCHEDULED
    assert (await InvasionService.get_current_wave(db_session, invasion.id)).status == (
        InvasionStatus.SCHEDULED
    )


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_duplicate_joins_return_one_participant_on_row_lock_database(
    db_session,
):
    group = await create_test_group(db_session, "Invasion concurrent join group")
    user = await create_test_user(db_session, "Concurrent join participant")
    invasion = await _start_invasion(db_session, group)
    invasion_id, user_id, group_id = invasion.id, user.id, group.id
    await db_session.commit()
    session_factory = get_async_sessionmaker()

    async def join_once():
        async with session_factory() as session:
            participant = await InvasionService.join(
                session,
                invasion_id,
                user_id,
                expected_group_id=group_id,
                now=NOW,
            )
            await session.commit()
            return participant.id

    participant_ids = await asyncio.gather(join_once(), join_once())
    assert participant_ids[0] == participant_ids[1]
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    ) == 1


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_duplicate_contributions_are_applied_once_on_row_lock_database(
    db_session,
):
    group = await create_test_group(db_session, "Invasion concurrent contribution group")
    user = await create_test_user(db_session, "Concurrent contribution participant")
    invasion = await _start_invasion(db_session, group)
    participant = await _join(db_session, invasion, user)
    invasion_id, participant_id = invasion.id, participant.id
    await db_session.commit()
    session_factory = get_async_sessionmaker()

    async def contribute_once():
        async with session_factory() as session:
            result = await InvasionService.record_contribution(
                session,
                invasion_id,
                participant_id,
                1,
                9,
                idempotency_key="concurrent:request",
                now=NOW,
            )
            await session.commit()
            return result

    results = await asyncio.gather(contribute_once(), contribute_once())
    assert sorted(result.duplicate for result in results) == [False, True]
    assert all(result.participant_total == 9 for result in results)
    assert all(result.wave_total == 9 for result in results)

    persisted_participant = await db_session.get(
        InvasionParticipant, participant_id
    )
    persisted_wave = await db_session.scalar(
        select(InvasionWave).where(
            InvasionWave.invasion_id == invasion_id,
            InvasionWave.wave_sequence == 1,
        )
    )
    assert persisted_participant.contribution_total == 9
    assert persisted_wave.contribution_total == 9
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_resolution_returns_one_persisted_outcome_on_row_lock_database(
    db_session,
):
    group = await create_test_group(db_session, "Invasion concurrent resolve group")
    user = await create_test_user(db_session, "Concurrent resolve participant")
    invasion = await _start_invasion(db_session, group)
    participant = await _join(db_session, invasion, user)
    await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        1,
        80,
        idempotency_key="concurrent:resolution",
        now=NOW,
    )
    await InvasionService.finish_wave(
        db_session, invasion.id, 1, now=NOW + timedelta(seconds=1)
    )
    invasion_id = invasion.id
    await db_session.commit()
    session_factory = get_async_sessionmaker()

    async def resolve_once():
        async with session_factory() as session:
            result = await InvasionService.resolve(
                session, invasion_id, now=NOW + timedelta(seconds=2)
            )
            await session.commit()
            return result

    results = await asyncio.gather(resolve_once(), resolve_once())
    assert [result.outcome for result in results] == [
        InvasionOutcome.NARROW_DEFENSE,
        InvasionOutcome.NARROW_DEFENSE,
    ]
    assert sorted(result.duplicate for result in results) == [False, True]

    persisted = await db_session.get(Invasion, invasion_id)
    await db_session.refresh(persisted)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome == InvasionOutcome.NARROW_DEFENSE
