"""Focused tests for completed-Invasion reward settlement."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    InvasionDefenseAction,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
    PlayerProgression,
    PlayerXPGrant,
    SeedlingAward,
    SeedlingInventory,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.config_store import ConfigService
from sib.services.invasion import InvasionNotFoundError, InvasionService
from sib.services.invasion_rewards import (
    InvasionNotCompletedError,
    InvasionRewardLedgerConflictError,
    InvasionRewardService,
    InvasionSettlementOutcomeError,
)
from sib.services.player_cards import PlayerCardService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_group, create_test_user


UTC = timezone.utc
NOW = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)


async def _setup_invasion(
    session,
    *,
    outcome=InvasionOutcome.TOTAL_VICTORY,
    status=InvasionStatus.FINISHED,
    user_count=1,
    join_count=None,
    with_main_card=False,
):
    group = await create_test_group(session, f"Invasion rewards {uuid.uuid4().hex[:8]}")
    users = [
        await create_test_user(session, f"Reward player {uuid.uuid4().hex[:8]}")
        for _ in range(user_count)
    ]

    card = None
    character = None
    if with_main_card:
        character = await CharacterService.create(
            session,
            code=f"reward_card_{uuid.uuid4().hex[:10]}",
            name_fa="کارت آزمایشی پاداش",
            name_en="Invasion Reward Test Card",
            artwork="invasion-reward-test.png",
            power=24,
            speed=18,
            intelligence=18,
        )
        card = await PlayerCardService.acquire(session, users[0].id, character.id)
        await PlayerCardService.set_favorite(
            session,
            users[0].id,
            card.id,
            expected_main_card_id=0,
        )

    invasion = await InvasionService.create(
        session,
        group_id=group.id,
        occurrence_key=f"invasion-reward:{uuid.uuid4().hex}",
        scheduled_at=NOW - timedelta(minutes=5),
        enemy_faction_code="moon_descendants",
        enemy_power=500,
        event_config={"test": "settlement-v1"},
        wave_compositions=[{"enemy": "test-wave", "power": 500}],
    )

    if status != InvasionStatus.SCHEDULED:
        await InvasionService.start(
            session,
            invasion.id,
            now=NOW,
            deadline_at=NOW + timedelta(minutes=10),
        )

    if join_count is None:
        join_count = user_count
    for user in users[:join_count]:
        await InvasionService.join(
            session,
            invasion.id,
            user.id,
            expected_group_id=group.id,
            now=NOW,
        )

    if status == InvasionStatus.FINISHED:
        # Settlement consumes persisted lifecycle/outcome state; it must not
        # invoke resolve() or derive an outcome from these zero contribution
        # counters. Mark the test aggregate terminal without running combat.
        waves = list(
            (
                await session.scalars(
                    select(InvasionWave).where(InvasionWave.invasion_id == invasion.id)
                )
            ).all()
        )
        for wave in waves:
            wave.status = InvasionStatus.FINISHED
            wave.ended_at = NOW
            wave.next_transition_at = None
        invasion.status = InvasionStatus.FINISHED
        invasion.outcome = outcome
        invasion.ended_at = NOW
        invasion.current_wave_sequence = None
        invasion.next_transition_at = None

    await session.flush()
    await session.commit()
    participants = list(
        (
            await session.scalars(
                select(InvasionParticipant)
                .where(InvasionParticipant.invasion_id == invasion.id)
                .order_by(InvasionParticipant.user_id.asc())
            )
        ).all()
    )
    return group, users, invasion, participants, card, character


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_apples"),
    [
        (InvasionOutcome.TOTAL_VICTORY, 10),
        (InvasionOutcome.NARROW_DEFENSE, 5),
        (InvasionOutcome.DEFEAT, 1),
    ],
)
async def test_persisted_outcome_selects_v1_reward_for_each_participant(
    db_session, outcome, expected_apples
):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session,
        outcome=outcome,
    )
    participant = participants[0]
    assert participant.user_id == users[0].id
    assert participant.contribution_total == 0
    assert participant.main_card_id_snapshot is None
    assert participant.power_snapshot is None
    assert invasion.enemy_power == 500
    assert invasion.outcome == outcome

    # Neither a Main Card nor a Brace is an eligibility requirement, and the
    # contradictory zero contribution must not cause settlement to recalculate
    # the persisted TOTAL_VICTORY result.
    result = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()

    assert result.outcome == outcome
    assert result.participant_count == 1
    assert result.newly_settled_participants == 1
    assert result.newly_granted_apples == expected_apples
    assert result.participant_rewards[0].apples == expected_apples
    assert result.participant_rewards[0].already_settled is False
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion.id
        )
    ) == 0

    wallet = await db_session.get(AppleWallet, users[0].id)
    ledger = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.user_id == users[0].id,
            AppleTransaction.idempotency_key
            == f"invasion:{invasion.id}:participant:{participant.id}:apples",
        )
    )
    assert wallet is not None and wallet.balance == expected_apples
    assert ledger is not None
    assert ledger.amount == expected_apples
    assert ledger.reason == "invasion_reward"
    assert ledger.ref_type == "invasion"
    assert ledger.ref_id == invasion.id
    assert await db_session.get(PlayerProgression, users[0].id) is None
    assert await db_session.scalar(select(func.count(PlayerXPGrant.id))) == 0
    assert await db_session.get(SeedlingInventory, users[0].id) is None
    assert await db_session.scalar(select(func.count(SeedlingAward.id))) == 0


@pytest.mark.asyncio
async def test_nonparticipant_receives_nothing_and_is_not_in_settlement_result(db_session):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session,
        user_count=2,
        join_count=1,
    )
    assert len(participants) == 1

    result = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()

    assert [reward.user_id for reward in result.participant_rewards] == [users[0].id]
    assert await db_session.get(AppleWallet, users[0].id) is not None
    assert await db_session.get(AppleWallet, users[1].id) is None
    assert await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.user_id == users[1].id,
            AppleTransaction.ref_type == "invasion",
            AppleTransaction.ref_id == invasion.id,
        )
    ) == 0


@pytest.mark.asyncio
async def test_finished_invasion_without_participants_settles_as_a_noop(db_session):
    _group, _users, invasion, participants, _card, _character = await _setup_invasion(
        db_session,
        user_count=0,
        join_count=0,
    )
    assert participants == []

    result = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()

    assert result.outcome == InvasionOutcome.TOTAL_VICTORY
    assert result.participant_count == 0
    assert result.total_apples == 0
    assert result.newly_settled_participants == 0
    assert result.newly_granted_apples == 0
    assert result.duplicate is True
    assert await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.ref_type == "invasion",
            AppleTransaction.ref_id == invasion.id,
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(AppleWallet.user_id))
    ) == 0


@pytest.mark.asyncio
async def test_configured_reward_changed_before_first_settlement_is_used(db_session):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session
    )
    await ConfigService.set(
        db_session,
        "invasion_reward_total_victory_apples",
        17,
    )
    await db_session.commit()

    result = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()

    assert result.newly_granted_apples == 17
    assert result.participant_rewards[0].apples == 17
    wallet = await db_session.get(AppleWallet, users[0].id)
    assert wallet is not None and wallet.balance == 17
    ledger = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.idempotency_key
            == f"invasion:{invasion.id}:participant:{participants[0].id}:apples"
        )
    )
    assert ledger is not None and ledger.amount == 17


@pytest.mark.asyncio
async def test_conflicting_reward_ledger_payload_is_not_treated_as_settled(db_session):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session
    )
    invasion_id = int(invasion.id)
    user_id = int(users[0].id)
    participant_id = int(participants[0].id)
    key = f"invasion:{invasion_id}:participant:{participant_id}:apples"
    await AppleWalletService.earn(
        db_session,
        user_id,
        7,
        reason="unrelated_reward",
        ref_type="invasion",
        ref_id=invasion_id,
        idempotency_key=key,
    )
    await db_session.commit()

    with pytest.raises(InvasionRewardLedgerConflictError):
        await InvasionRewardService.settle_completed_invasion(
            db_session, invasion_id
        )
    await db_session.rollback()

    ledger = await db_session.scalar(
        select(AppleTransaction).where(AppleTransaction.idempotency_key == key)
    )
    assert ledger is not None and ledger.reason == "unrelated_reward"
    wallet = await db_session.get(AppleWallet, user_id)
    assert wallet is not None and wallet.balance == 7
    assert await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.ref_type == "invasion",
            AppleTransaction.ref_id == invasion_id,
        )
    ) == 1


@pytest.mark.asyncio
async def test_repeated_settlement_uses_apple_ledger_idempotency(db_session):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session,
        user_count=2,
    )

    participants_by_user = {participant.user_id: participant for participant in participants}
    first = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()
    second = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()

    assert first.duplicate is False
    assert first.newly_granted_apples == 20
    assert second.duplicate is True
    assert second.newly_settled_participants == 0
    assert second.newly_granted_apples == 0
    assert second.total_apples == 20
    for user in users:
        participant = participants_by_user[user.id]
        assert await db_session.scalar(
            select(func.count(AppleTransaction.id)).where(
                AppleTransaction.user_id == user.id,
                AppleTransaction.idempotency_key
                == f"invasion:{invasion.id}:participant:{participant.id}:apples",
            )
        ) == 1
        wallet = await db_session.get(AppleWallet, user.id)
        assert wallet.balance == 10


@pytest.mark.asyncio
async def test_partial_settlement_retry_pays_missing_participant_once_and_keeps_amount(
    db_session, monkeypatch
):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session,
        user_count=2,
    )
    participants_by_user = {participant.user_id: participant for participant in participants}
    ordered = sorted(
        ((user, participants_by_user[user.id]) for user in users),
        key=lambda item: item[0].id,
    )
    first_user = ordered[0][0]
    second_user = ordered[1][0]
    original_earn = AppleWalletService.earn

    async def fail_on_second_user(session, *, user_id, **kwargs):
        if user_id == second_user.id:
            raise RuntimeError("injected failure after the first participant reward")
        return await original_earn(session, user_id=user_id, **kwargs)

    monkeypatch.setattr(
        AppleWalletService,
        "earn",
        staticmethod(fail_on_second_user),
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        await InvasionRewardService.settle_completed_invasion(db_session, invasion.id)

    # Model a partial settlement already durable in the ledger (for example,
    # a later reward channel failing after this participant committed).
    await db_session.commit()
    assert await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.user_id == first_user.id,
            AppleTransaction.ref_id == invasion.id,
            AppleTransaction.ref_type == "invasion",
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.user_id == second_user.id,
            AppleTransaction.ref_id == invasion.id,
            AppleTransaction.ref_type == "invasion",
        )
    ) == 0

    # Runtime balance tuning after the partial payout must not alter the
    # amount of the remaining participants on retry.
    monkeypatch.undo()
    await ConfigService.set(db_session, "invasion_reward_total_victory_apples", 99)
    await db_session.commit()

    retry = await InvasionRewardService.settle_completed_invasion(db_session, invasion.id)
    await db_session.commit()

    assert retry.duplicate is False
    assert retry.newly_settled_participants == 1
    assert retry.newly_granted_apples == 10
    for user, participant in ordered:
        ledger = await db_session.scalar(
            select(AppleTransaction).where(
                AppleTransaction.user_id == user.id,
                AppleTransaction.idempotency_key
                == f"invasion:{invasion.id}:participant:{participant.id}:apples",
            )
        )
        assert ledger is not None and ledger.amount == 10
        wallet = await db_session.get(AppleWallet, user.id)
        assert wallet.balance == 10


@pytest.mark.asyncio
async def test_settlement_does_not_commit_and_caller_can_rollback(db_session):
    _group, users, invasion, _participants, _card, _character = await _setup_invasion(
        db_session
    )
    user_id = users[0].id
    invasion_id = invasion.id

    await InvasionRewardService.settle_completed_invasion(db_session, invasion_id)
    assert await db_session.get(AppleWallet, user_id) is not None
    await db_session.rollback()

    assert await db_session.get(AppleWallet, user_id) is None
    assert await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.user_id == user_id,
            AppleTransaction.ref_type == "invasion",
            AppleTransaction.ref_id == invasion_id,
        )
    ) == 0

    await InvasionRewardService.settle_completed_invasion(db_session, invasion_id)
    await db_session.commit()
    assert (await db_session.get(AppleWallet, user_id)).balance == 10


@pytest.mark.asyncio
async def test_reward_is_stable_after_live_main_card_changes(db_session, monkeypatch):
    _group, users, invasion, participants, original_card, original_character = (
        await _setup_invasion(db_session, with_main_card=True)
    )
    participant = participants[0]
    original_card_id = original_card.id
    snapshot_power = participant.power_snapshot
    assert snapshot_power == 24
    assert participant.main_card_id_snapshot == original_card_id

    # The event is already finished. Upgrade/rebalance the old Main Card and
    # select a different one; settlement must use the persisted outcome and
    # participant, not a live card lookup or current character values.
    original_card.level = 9
    original_character.power = 60
    replacement_character = await CharacterService.create(
        db_session,
        code=f"replacement_{uuid.uuid4().hex[:10]}",
        name_fa="کارت جایگزین",
        name_en="Replacement Card",
        artwork="replacement-card.png",
        power=20,
        speed=20,
        intelligence=20,
    )
    replacement_card = await PlayerCardService.acquire(
        db_session, users[0].id, replacement_character.id
    )
    await PlayerCardService.set_favorite(
        db_session,
        users[0].id,
        replacement_card.id,
        expected_main_card_id=original_card_id,
    )
    await db_session.commit()
    monkeypatch.setattr(
        PlayerCardService,
        "get_main_card",
        staticmethod(AsyncFailure("live Main Card lookup is forbidden")),
    )

    result = await InvasionRewardService.settle_completed_invasion(
        db_session, invasion.id
    )
    await db_session.commit()

    assert result.participant_rewards[0].apples == 10
    assert participant.power_snapshot == snapshot_power
    assert replacement_card.is_favorite is True


class AsyncFailure:
    """Async callable that fails if settlement consults mutable card state."""

    def __init__(self, message):
        self.message = message

    async def __call__(self, *args, **kwargs):
        raise AssertionError(self.message)


@pytest.mark.asyncio
async def test_settlement_rejects_missing_scheduled_active_and_unresolved_invasions(
    db_session,
):
    _group, _users, scheduled, _participants, _card, _character = await _setup_invasion(
        db_session,
        status=InvasionStatus.SCHEDULED,
        user_count=0,
        join_count=0,
    )
    with pytest.raises(InvasionNotCompletedError):
        await InvasionRewardService.settle_completed_invasion(db_session, scheduled.id)
    assert scheduled.status == InvasionStatus.SCHEDULED

    _group, _users, active, _participants, _card, _character = await _setup_invasion(
        db_session,
        status=InvasionStatus.ACTIVE,
        user_count=0,
        join_count=0,
    )
    with pytest.raises(InvasionNotCompletedError):
        await InvasionRewardService.settle_completed_invasion(db_session, active.id)
    assert active.status == InvasionStatus.ACTIVE
    assert active.outcome is None

    _group, _users, unresolved, _participants, _card, _character = await _setup_invasion(
        db_session,
        outcome=None,
        status=InvasionStatus.FINISHED,
        user_count=0,
        join_count=0,
    )
    with pytest.raises(InvasionSettlementOutcomeError):
        await InvasionRewardService.settle_completed_invasion(db_session, unresolved.id)
    assert unresolved.status == InvasionStatus.FINISHED
    assert unresolved.outcome is None

    with pytest.raises(InvasionNotFoundError):
        await InvasionRewardService.settle_completed_invasion(
            db_session, 9_223_372_036_854_775_807
        )


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_settlement_attempts_grant_each_participant_once(db_session):
    _group, users, invasion, participants, _card, _character = await _setup_invasion(
        db_session,
        user_count=2,
    )
    session_maker = get_async_sessionmaker()

    async def settle_once():
        async with session_maker() as session:
            result = await InvasionRewardService.settle_completed_invasion(
                session, invasion.id
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(settle_once(), settle_once())
    assert sorted([first.duplicate, second.duplicate]) == [False, True]

    participants_by_user = {participant.user_id: participant for participant in participants}
    for user in users:
        participant = participants_by_user[user.id]
        assert await db_session.scalar(
            select(func.count(AppleTransaction.id)).where(
                AppleTransaction.user_id == user.id,
                AppleTransaction.idempotency_key
                == f"invasion:{invasion.id}:participant:{participant.id}:apples",
            )
        ) == 1
        wallet = await db_session.get(AppleWallet, user.id)
        assert wallet is not None and wallet.balance == 10
