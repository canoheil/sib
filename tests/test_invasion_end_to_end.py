"""Service-boundary restart test for a scheduled Invasion through settlement."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    AppleWallet,
    InvasionContribution,
    InvasionDefenseAction,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
)
from sib.services.invasion import InvasionDefenseActionCode, InvasionService
from sib.services.invasion_rewards import InvasionRewardService
from sib.services.items import ItemService
from sib.services.recovery import RecoveryService
from sib.tests.test_parties import create_test_group, create_test_user

UTC = timezone.utc
START_AT = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _recover_after_restart(session_maker, now: datetime) -> dict[str, int]:
    """Run startup recovery with a fresh session, as a restarted process would."""
    async with session_maker() as session:
        return await RecoveryService.recover_on_startup(session, now=now)


async def _create_legacy_scheduled_invasion(session, group_id: int) -> int:
    """Persist a Phase-5 two-wave occurrence directly (no current scheduler).

    The scheduler now activates battles instead of pre-creating them, so the
    legacy timeline is exercised by constructing the old row shape explicitly.
    """
    invasion = await InvasionService.create(
        session,
        group_id=group_id,
        occurrence_key=f"legacy-e2e:group:{group_id}:slot:{int(START_AT.timestamp())}",
        scheduled_at=START_AT,
        enemy_faction_code="moon_descendants",
        enemy_power=100,
        event_config={
            "schema_version": 1,
            "wave_count": 2,
            "wave_durations_seconds": [600, 600],
        },
        wave_compositions=[
            {"wave_sequence": 1, "enemy_power": 50},
            {"wave_sequence": 2, "enemy_power": 50},
        ],
    )
    await session.commit()
    return int(invasion.id)


@pytest.mark.asyncio
async def test_legacy_scheduled_invasion_recovers_through_outcome_and_golden_apple_settlement(
    db_session,
):
    group = await create_test_group(db_session, "Invasion end-to-end recovery")
    user = await create_test_user(db_session, "Invasion end-to-end defender")
    group_id, user_id = int(group.id), int(user.id)
    session_maker = get_async_sessionmaker()

    invasion_id = await _create_legacy_scheduled_invasion(db_session, group_id)
    async with session_maker() as session:
        invasion = await InvasionService.get(session, invasion_id)
        assert invasion.status == InvasionStatus.SCHEDULED
        assert _utc(invasion.scheduled_at) == START_AT
        assert await session.scalar(
            select(func.count(InvasionWave.id)).where(
                InvasionWave.invasion_id == invasion_id
            )
        ) == 2

    # A restart just before the scheduled time leaves the occurrence untouched.
    early_recovery = await _recover_after_restart(
        session_maker, START_AT - timedelta(seconds=1)
    )
    assert early_recovery["invasions_started"] == 0
    async with session_maker() as session:
        invasion = await InvasionService.get(session, invasion_id)
        assert invasion.status == InvasionStatus.SCHEDULED

    # Recovery at the due time starts the event and first persisted Wave.
    start_recovery = await _recover_after_restart(session_maker, START_AT)
    assert start_recovery["invasions_started"] == 1
    async with session_maker() as session:
        invasion = await InvasionService.get(session, invasion_id)
        wave = await InvasionService.get_current_wave(session, invasion_id)
        assert invasion.status == InvasionStatus.ACTIVE
        assert invasion.enemy_power == 100
        assert wave is not None
        assert wave.wave_sequence == 1
        assert wave.status == InvasionStatus.ACTIVE
        assert _utc(wave.deadline_at) == START_AT + timedelta(minutes=10)
        # Legacy events never receive the four-wave heartbeat or stage.
        assert invasion.heartbeat_at is None
        assert invasion.presentation_stage is None

    # Join, action, and generic contribution each persist through their own
    # caller-owned transaction, then survive a separate startup recovery.
    async with session_maker() as session:
        participant = await InvasionService.join(
            session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            now=START_AT + timedelta(seconds=1),
        )
        participant_id = int(participant.id)
        assert participant.main_card_id_snapshot is None
        await session.commit()

    action_key = "phase10-e2e:brace:wave-1"
    async with session_maker() as session:
        action = await InvasionService.perform_defense_action(
            session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            wave_sequence=1,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key=action_key,
            now=START_AT + timedelta(minutes=1),
        )
        assert action.duplicate is False
        assert action.contribution_amount == 1
        await session.commit()

    async with session_maker() as session:
        contribution = await InvasionService.record_contribution(
            session,
            invasion_id,
            participant_id,
            1,
            99,
            idempotency_key="phase10-e2e:manual:wave-1",
            now=START_AT + timedelta(minutes=2),
        )
        assert contribution.participant_total == 100
        assert contribution.wave_total == 100
        assert contribution.invasion_total == 100
        await session.commit()

    before_deadline = await _recover_after_restart(
        session_maker, START_AT + timedelta(minutes=5)
    )
    assert before_deadline["invasion_wave_transitions"] == 0

    # A retried callback key in a new session returns its original persisted
    # result rather than changing Wave or participant totals a second time.
    async with session_maker() as session:
        replay = await InvasionService.perform_defense_action(
            session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            wave_sequence=1,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key=action_key,
            now=START_AT + timedelta(minutes=6),
        )
        assert replay.duplicate is True
        assert replay.action_id == action.action_id
        await session.commit()

    async with session_maker() as session:
        participant = await session.get(InvasionParticipant, participant_id)
        wave = await InvasionService.get_current_wave(session, invasion_id)
        assert participant is not None and participant.contribution_total == 100
        assert wave is not None and wave.contribution_total == 100
        assert await session.scalar(
            select(func.count(InvasionDefenseAction.id)).where(
                InvasionDefenseAction.invasion_id == invasion_id
            )
        ) == 1
        assert await session.scalar(
            select(func.count(InvasionContribution.id)).where(
                InvasionContribution.invasion_id == invasion_id
            )
        ) == 2

    # Recovery deliberately advances only one persisted lifecycle transition
    # per pass: Wave 1 finishes first, then Wave 2 starts on the next restart.
    wave_one_recovery = await _recover_after_restart(
        session_maker, START_AT + timedelta(minutes=10)
    )
    assert wave_one_recovery["invasion_wave_transitions"] == 1
    assert wave_one_recovery["invasions_resolved"] == 0
    async with session_maker() as session:
        invasion = await InvasionService.get(session, invasion_id)
        wave = await InvasionService.get_current_wave(session, invasion_id)
        assert invasion.status == InvasionStatus.ACTIVE
        assert invasion.current_wave_sequence == 2
        assert wave is not None and wave.status == InvasionStatus.SCHEDULED
        assert _utc(wave.next_transition_at) == START_AT + timedelta(minutes=10)

    wave_two_recovery = await _recover_after_restart(
        session_maker, START_AT + timedelta(minutes=10)
    )
    assert wave_two_recovery["invasion_wave_transitions"] == 1
    async with session_maker() as session:
        wave = await InvasionService.get_current_wave(session, invasion_id)
        assert wave is not None and wave.status == InvasionStatus.ACTIVE
        assert _utc(wave.deadline_at) == START_AT + timedelta(minutes=20)

    async with session_maker() as session:
        second_wave_action = await InvasionService.perform_defense_action(
            session,
            invasion_id,
            user_id,
            expected_group_id=group_id,
            wave_sequence=2,
            action_code=InvasionDefenseActionCode.BRACE,
            idempotency_key="phase10-e2e:brace:wave-2",
            now=START_AT + timedelta(minutes=11),
        )
        assert second_wave_action.duplicate is False
        assert second_wave_action.contribution_amount == 1
        await session.commit()

    no_early_finish = await _recover_after_restart(
        session_maker, START_AT + timedelta(minutes=15)
    )
    assert no_early_finish["invasion_wave_transitions"] == 0

    # The final recovery persists the result and settles it in the same
    # maintenance pass: the defender receives Golden Apples exactly once.
    finish_recovery = await _recover_after_restart(
        session_maker, START_AT + timedelta(minutes=20)
    )
    assert finish_recovery["invasion_wave_transitions"] == 1
    assert finish_recovery["invasions_resolved"] == 1
    assert finish_recovery["invasion_rewards_settled"] == 1
    async with session_maker() as session:
        invasion = await InvasionService.get(session, invasion_id)
        assert invasion.status == InvasionStatus.FINISHED
        assert invasion.outcome == InvasionOutcome.TOTAL_VICTORY
        assert _utc(invasion.ended_at) == START_AT + timedelta(minutes=20)
        assert invasion.current_wave_sequence is None
        assert invasion.rewards_settled_at is not None
        assert await session.scalar(
            select(func.sum(InvasionWave.contribution_total)).where(
                InvasionWave.invasion_id == invasion_id
            )
        ) == 101
        assert await ItemService.get_total(session, user_id, "apple_gold") == 10

    # Repeated restarts neither change the result nor pay again.
    terminal_recovery = await _recover_after_restart(
        session_maker, START_AT + timedelta(minutes=20)
    )
    assert terminal_recovery["invasions_resolved"] == 0
    assert terminal_recovery["invasion_rewards_settled"] == 0

    async with session_maker() as session:
        settlement = await InvasionRewardService.settle_completed_invasion(
            session, invasion_id
        )
        assert settlement.outcome == InvasionOutcome.TOTAL_VICTORY
        assert settlement.duplicate is True
        assert settlement.newly_granted_golden_apples == 0
        await session.commit()

    # Post-settlement replay after a fresh restart remains ledger-idempotent.
    await _recover_after_restart(session_maker, START_AT + timedelta(minutes=20))
    async with session_maker() as session:
        duplicate = await InvasionRewardService.settle_completed_invasion(
            session, invasion_id
        )
        assert duplicate.duplicate is True
        assert duplicate.newly_granted_golden_apples == 0
        assert await ItemService.get_total(session, user_id, "apple_gold") == 10
        # The Apple wallet is never the reward currency.
        wallet = await session.get(AppleWallet, user_id)
        assert wallet is None or wallet.balance == 0
        await session.commit()
