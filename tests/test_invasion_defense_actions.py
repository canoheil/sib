"""Focused tests for the transactional Invasion defense-action engine."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    InvasionContribution,
    InvasionDefenseAction,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
)
from sib.services.characters import CharacterService
from sib.services.invasion import (
    BRACE_BASE_CONTRIBUTION,
    BRACE_POWER_DIVISOR,
    InvasionAlreadyResolvedError,
    InvasionDefenseActionAlreadyUsedError,
    InvasionDefenseActionCode,
    InvasionDefenseActionConflictError,
    InvasionGroupMismatchError,
    InvasionInvalidDefenseActionError,
    InvasionInvalidWaveError,
    InvasionNotActiveError,
    InvasionNotFoundError,
    InvasionNotParticipantError,
    InvasionService,
    InvasionStaleWaveError,
)
from sib.services.player_cards import PlayerCardService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_group, create_test_user


UTC = timezone.utc
NOW = datetime(2026, 10, 7, 12, 30, tzinfo=UTC)


async def _setup_active_invasion(
    session,
    *,
    with_main_card: bool = False,
    wave_count: int = 1,
    deadline_at: datetime | None = None,
):
    group = await create_test_group(session, f"Defense action group {uuid.uuid4().hex[:8]}")
    user = await create_test_user(session, f"Defense player {uuid.uuid4().hex[:8]}")

    card = None
    character = None
    if with_main_card:
        character = await CharacterService.create(
            session,
            code=f"defense_{uuid.uuid4().hex[:10]}",
            name_fa="مدافع یورش",
            name_en="Invasion Defender",
            artwork="invasion-defender.png",
            power=24,
            speed=18,
            intelligence=18,
        )
        card = await PlayerCardService.acquire(session, user.id, character.id)
        await PlayerCardService.set_favorite(
            session,
            user.id,
            card.id,
            expected_main_card_id=0,
        )

    invasion = await InvasionService.create(
        session,
        group_id=group.id,
        occurrence_key=f"defense-action:{uuid.uuid4().hex}",
        scheduled_at=NOW - timedelta(minutes=1),
        enemy_faction_code="moon_descendants",
        enemy_power=1000,
        event_config={"test": "defense-action-v1"},
        wave_compositions=[
            {"wave_sequence": sequence, "enemy_power": 100}
            for sequence in range(1, wave_count + 1)
        ],
    )
    deadline_at = deadline_at or NOW + timedelta(minutes=10)
    await InvasionService.start(
        session,
        invasion.id,
        now=NOW,
        deadline_at=deadline_at,
    )
    participant = await InvasionService.join(
        session,
        invasion.id,
        user.id,
        expected_group_id=group.id,
        now=NOW,
    )
    return group, user, invasion, participant, card, character


async def _perform(
    session,
    invasion,
    user,
    *,
    wave_sequence: int = 1,
    action_code: str = InvasionDefenseActionCode.BRACE,
    idempotency_key: str = "telegram-callback:brace:one",
    expected_group_id: int | None = None,
    now: datetime = NOW,
):
    return await InvasionService.perform_defense_action(
        session,
        invasion.id,
        user.id,
        expected_group_id=(
            invasion.group_id if expected_group_id is None else expected_group_id
        ),
        wave_sequence=wave_sequence,
        action_code=action_code,
        idempotency_key=idempotency_key,
        now=now,
    )


@pytest.mark.asyncio
async def test_brace_action_uses_snapshot_formula_and_existing_contribution_ledger(
    db_session,
):
    _group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
    )
    # V1 brace formula: 1 + floor(join-snapshot power / 10); power 24 -> 3.
    expected_amount = BRACE_BASE_CONTRIBUTION + (
        participant.power_snapshot // BRACE_POWER_DIVISOR
    )

    result = await _perform(db_session, invasion, user)

    assert result.duplicate is False
    assert result.action_code == InvasionDefenseActionCode.BRACE
    assert result.power_snapshot_used == participant.power_snapshot == 24
    assert result.contribution_amount == expected_amount == 3
    assert result.participant_total_after == expected_amount
    assert result.wave_total_after == expected_amount

    action = await db_session.scalar(
        select(InvasionDefenseAction).where(InvasionDefenseAction.id == result.action_id)
    )
    contribution = await db_session.scalar(
        select(InvasionContribution).where(
            InvasionContribution.invasion_id == invasion.id,
            InvasionContribution.idempotency_key == result.idempotency_key,
        )
    )
    wave = await InvasionService.get_current_wave(db_session, invasion.id)
    assert action is not None
    assert action.contribution_id == contribution.id
    assert contribution.participant_id == participant.id
    assert contribution.wave_id == wave.id
    assert contribution.amount == expected_amount
    assert participant.contribution_total == expected_amount
    assert wave.contribution_total == expected_amount
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion.id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion.id
        )
    ) == 1


@pytest.mark.asyncio
async def test_action_uses_join_snapshot_after_live_card_and_character_change(
    db_session,
    monkeypatch,
):
    _group, user, invasion, participant, card, character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
    )
    snapshotted_power = participant.power_snapshot
    assert card is not None and character is not None

    # Simulate a later upgrade and Character rebalance. The action engine must
    # not consult either live row or reselect a Main Card.
    card.level = 9
    card.is_favorite = False
    character.power = 900
    await db_session.flush()
    monkeypatch.setattr(
        PlayerCardService,
        "get_main_card",
        AsyncMock(side_effect=AssertionError("live Main Card lookup is forbidden")),
    )

    result = await _perform(db_session, invasion, user)

    assert result.power_snapshot_used == snapshotted_power == 24
    assert result.contribution_amount == BRACE_BASE_CONTRIBUTION + 24 // 10 == 3


@pytest.mark.asyncio
async def test_participant_without_main_card_can_brace_without_fallback_selection(db_session):
    _group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=False,
    )
    assert participant.main_card_id_snapshot is None
    assert participant.power_snapshot is None

    result = await _perform(db_session, invasion, user)

    assert result.power_snapshot_used is None
    assert result.contribution_amount == BRACE_BASE_CONTRIBUTION == 1
    assert participant.contribution_total == 1
    assert await PlayerCardService.get_main_card(db_session, user.id) is None


@pytest.mark.asyncio
async def test_brace_is_limited_to_once_per_wave_and_resets_for_next_wave(db_session):
    _group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        wave_count=2,
    )
    first = await _perform(
        db_session,
        invasion,
        user,
        idempotency_key="wave-one-first-brace",
    )
    with pytest.raises(InvasionDefenseActionAlreadyUsedError):
        await _perform(
            db_session,
            invasion,
            user,
            idempotency_key="wave-one-distinct-brace",
        )
    assert participant.contribution_total == first.contribution_amount == 1

    await InvasionService.finish_wave(
        db_session,
        invasion.id,
        1,
        now=NOW + timedelta(minutes=10),
    )
    await InvasionService.advance_wave(
        db_session,
        invasion.id,
        2,
        now=NOW + timedelta(minutes=10),
        deadline_at=NOW + timedelta(minutes=20),
    )
    second = await _perform(
        db_session,
        invasion,
        user,
        wave_sequence=2,
        idempotency_key="wave-two-brace",
        now=NOW + timedelta(minutes=10, seconds=1),
    )
    assert second.wave_sequence == 2
    assert second.contribution_amount == 1
    assert participant.contribution_total == 2
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.participant_id == participant.id
        )
    ) == 2


@pytest.mark.asyncio
async def test_same_action_key_replays_stably_and_conflicting_payload_is_rejected(
    db_session,
):
    _group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
        wave_count=2,
    )
    first = await _perform(db_session, invasion, user)
    replay = await _perform(db_session, invasion, user)

    assert first.duplicate is False
    assert replay.duplicate is True
    assert replay.action_id == first.action_id
    assert replay.contribution_amount == first.contribution_amount
    assert replay.participant_total_after == first.participant_total_after
    assert replay.wave_total_after == first.wave_total_after

    with pytest.raises(InvasionDefenseActionConflictError):
        await _perform(
            db_session,
            invasion,
            user,
            wave_sequence=2,
        )
    with pytest.raises(InvasionDefenseActionConflictError):
        await _perform(
            db_session,
            invasion,
            user,
            action_code="rally",
        )

    await InvasionService.finish_wave(
        db_session,
        invasion.id,
        1,
        now=NOW + timedelta(minutes=10),
    )
    await InvasionService.advance_wave(
        db_session,
        invasion.id,
        2,
        now=NOW + timedelta(minutes=10),
        deadline_at=NOW + timedelta(minutes=20),
    )
    await InvasionService.finish_wave(
        db_session,
        invasion.id,
        2,
        now=NOW + timedelta(minutes=20),
    )
    await InvasionService.resolve(
        db_session,
        invasion.id,
        now=NOW + timedelta(minutes=20),
    )
    finished_replay = await _perform(db_session, invasion, user)
    assert finished_replay.duplicate is True
    assert finished_replay.action_id == first.action_id
    assert finished_replay.contribution_amount == first.contribution_amount
    assert participant.contribution_total == first.contribution_amount

    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion.id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion.id
        )
    ) == 1


@pytest.mark.asyncio
async def test_retry_in_new_session_after_commit_returns_original_action_result(
    db_session,
):
    group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
    )
    invasion_id, user_id, group_id = invasion.id, user.id, group.id
    first = await _perform(
        db_session,
        invasion,
        user,
        idempotency_key="committed-action-retry",
    )
    await db_session.commit()

    session_factory = get_async_sessionmaker()
    async with session_factory() as retry_session:
        replay = await InvasionService.perform_defense_action(
            retry_session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            wave_sequence=1,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key="committed-action-retry",
            now=NOW + timedelta(seconds=5),
        )

    assert replay.duplicate is True
    assert replay.action_id == first.action_id
    assert replay.contribution_amount == first.contribution_amount
    assert replay.participant_total_after == first.participant_total_after
    assert replay.wave_total_after == first.wave_total_after
    assert replay.created_at == first.created_at
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1
    assert participant.contribution_total == first.contribution_amount


@pytest.mark.asyncio
async def test_defense_action_changes_rollback_atomically_with_caller_transaction(
    db_session,
):
    _group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
    )
    invasion_id = invasion.id
    participant_id = participant.id
    await db_session.commit()

    result = await _perform(db_session, invasion, user)
    assert result.contribution_amount > 0
    assert participant.contribution_total == result.contribution_amount
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
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 0


@pytest.mark.asyncio
async def test_action_distinguishes_missing_invasion_participant_group_and_action(db_session):
    group, user, invasion, _participant, _card, _character = await _setup_active_invasion(
        db_session,
    )
    other_group = await create_test_group(db_session, "Wrong defense context")
    outsider = await create_test_user(db_session, "Defense outsider")

    with pytest.raises(InvasionNotFoundError):
        await InvasionService.perform_defense_action(
            db_session,
            9_999_999_999,
            user.id,
            expected_group_id=group.id,
            wave_sequence=1,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key="missing-invasion-action",
            now=NOW,
        )
    with pytest.raises(InvasionNotParticipantError):
        await _perform(db_session, invasion, outsider, idempotency_key="outsider-action")
    with pytest.raises(InvasionGroupMismatchError):
        await _perform(
            db_session,
            invasion,
            user,
            expected_group_id=other_group.id,
            idempotency_key="wrong-group-action",
        )
    with pytest.raises(InvasionInvalidDefenseActionError):
        await _perform(
            db_session,
            invasion,
            user,
            action_code="strike",
            idempotency_key="unsupported-action",
        )
    with pytest.raises(InvasionInvalidWaveError):
        await _perform(
            db_session,
            invasion,
            user,
            wave_sequence=0,
            idempotency_key="invalid-wave-sequence",
        )
    with pytest.raises(InvasionInvalidWaveError):
        await _perform(
            db_session,
            invasion,
            user,
            wave_sequence=42,
            idempotency_key="missing-wave",
        )


@pytest.mark.asyncio
async def test_action_rejects_scheduled_and_finished_invasions(db_session):
    group, user, invasion, _participant, _card, _character = await _setup_active_invasion(
        db_session,
    )

    # A participant is normally joined only while ACTIVE; make the persisted
    # status scheduled to verify the action API independently rejects it.
    invasion.status = InvasionStatus.SCHEDULED
    await db_session.flush()
    with pytest.raises(InvasionNotActiveError):
        await _perform(db_session, invasion, user, idempotency_key="not-active")

    invasion.status = InvasionStatus.ACTIVE
    await db_session.flush()
    await InvasionService.finish_wave(
        db_session,
        invasion.id,
        1,
        now=NOW + timedelta(minutes=10),
    )
    await InvasionService.resolve(
        db_session,
        invasion.id,
        now=NOW + timedelta(minutes=10),
    )
    with pytest.raises(InvasionAlreadyResolvedError):
        await _perform(db_session, invasion, user, idempotency_key="finished-event")


@pytest.mark.asyncio
async def test_action_rejects_future_previous_and_expired_waves(db_session):
    _group, user, invasion, _participant, _card, _character = await _setup_active_invasion(
        db_session,
        wave_count=2,
    )
    with pytest.raises(InvasionStaleWaveError):
        await _perform(
            db_session,
            invasion,
            user,
            wave_sequence=2,
            idempotency_key="future-wave-action",
        )

    await InvasionService.finish_wave(
        db_session,
        invasion.id,
        1,
        now=NOW + timedelta(seconds=1),
    )
    await InvasionService.advance_wave(
        db_session,
        invasion.id,
        2,
        now=NOW + timedelta(seconds=1),
        deadline_at=NOW + timedelta(minutes=10),
    )
    with pytest.raises(InvasionStaleWaveError):
        await _perform(
            db_session,
            invasion,
            user,
            wave_sequence=1,
            idempotency_key="previous-wave-action",
            now=NOW + timedelta(seconds=2),
        )

    _group, expired_user, expired_invasion, _participant, _card, _character = (
        await _setup_active_invasion(
            db_session,
            deadline_at=NOW + timedelta(seconds=1),
        )
    )
    with pytest.raises(InvasionStaleWaveError):
        await _perform(
            db_session,
            expired_invasion,
            expired_user,
            idempotency_key="expired-wave-action",
            now=NOW + timedelta(seconds=1),
        )


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_duplicate_actions_create_one_action_and_contribution(db_session):
    group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
    )
    invasion_id, user_id, group_id, participant_id = (
        invasion.id,
        user.id,
        group.id,
        participant.id,
    )
    await db_session.commit()
    session_factory = get_async_sessionmaker()

    async def perform_once():
        async with session_factory() as session:
            result = await InvasionService.perform_defense_action(
                session,
                invasion_id,
                user_id,
                expected_group_id=group_id,
                wave_sequence=1,
                action_code=InvasionDefenseActionCode.BRACE,
                idempotency_key="concurrent-defense-action",
                now=NOW,
            )
            await session.commit()
            return result

    results = await asyncio.gather(perform_once(), perform_once())
    assert sorted(result.duplicate for result in results) == [False, True]
    assert results[0].action_id == results[1].action_id
    assert results[0].contribution_amount == results[1].contribution_amount

    persisted_participant = await db_session.get(
        InvasionParticipant,
        participant_id,
        populate_existing=True,
    )
    persisted_wave = await db_session.scalar(
        select(InvasionWave).where(
            InvasionWave.invasion_id == invasion_id,
            InvasionWave.wave_sequence == 1,
        )
    )
    assert persisted_participant.contribution_total == results[0].contribution_amount
    assert persisted_wave.contribution_total == results[0].contribution_amount
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_distinct_braces_cannot_bypass_one_per_wave_policy(db_session):
    group, user, invasion, participant, _card, _character = await _setup_active_invasion(
        db_session,
        with_main_card=True,
    )
    invasion_id, user_id, group_id, participant_id = (
        invasion.id,
        user.id,
        group.id,
        participant.id,
    )
    await db_session.commit()
    session_factory = get_async_sessionmaker()

    async def perform_once(key: str):
        async with session_factory() as session:
            try:
                result = await InvasionService.perform_defense_action(
                    session,
                    invasion_id,
                    user_id,
                    expected_group_id=group_id,
                    wave_sequence=1,
                    action_code=InvasionDefenseActionCode.BRACE,
                    idempotency_key=key,
                    now=NOW,
                )
            except InvasionDefenseActionAlreadyUsedError:
                await session.rollback()
                return None
            await session.commit()
            return result

    results = await asyncio.gather(
        perform_once("concurrent-distinct-brace-a"),
        perform_once("concurrent-distinct-brace-b"),
    )
    accepted = [result for result in results if result is not None]
    rejected = [result for result in results if result is None]
    assert len(accepted) == 1
    assert len(rejected) == 1

    db_session.expire_all()
    persisted_participant = await db_session.get(
        InvasionParticipant,
        participant_id,
        populate_existing=True,
    )
    persisted_wave = await db_session.scalar(
        select(InvasionWave).where(
            InvasionWave.invasion_id == invasion_id,
            InvasionWave.wave_sequence == 1,
        )
    )
    assert persisted_participant.contribution_total == accepted[0].contribution_amount
    assert persisted_wave.contribution_total == accepted[0].contribution_amount
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1
