"""Tests for Matchmaking Queue, Cross-Group Enforcement, and State Machine."""

from datetime import datetime, timezone, timedelta
import pytest

from sib.tests.conftest import requires_row_locks
from sqlalchemy import select
from sib.core.config import settings
from sib.core.errors import InvalidActionException
from sib.database.models import (
    Party,
    PartyStatus,
    Match,
    MatchState,
    GroupStats,
    MatchmakingQueue,
)
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.services.timeout.service import TimeoutService
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_no_elo_or_rating_window_code_remains():
    """Elo/MMR must not exist in the active matchmaking path."""
    assert not hasattr(MatchmakingService, "calculate_rating_window")
    assert not hasattr(MatchmakingService, "INITIAL_RATING_WINDOW")
    assert not hasattr(MatchmakingService, "RATING_EXPANSION_RATE_PER_MINUTE")


@pytest.mark.asyncio
async def test_cross_group_enforcement_never_matches_same_group(db_session):
    """CRITICAL RULE: Two parties from the same Telegram group must NEVER match each other."""
    group = await create_test_group(db_session, "گروه مشترک")
    u1 = await create_test_user(db_session, "کاربر ۱")
    u2 = await create_test_user(db_session, "کاربر ۲")

    # Both parties in the same group
    party1 = await PartyService.create_party(db_session, group.id, u1.id, "rock_paper_scissors", "solo")
    party2 = await PartyService.create_party(db_session, group.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    # Enqueue party 1
    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    # Enqueue party 2
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    await db_session.commit()

    # Attempt to match party 1
    match = await MatchmakingService.find_and_create_match(db_session, party1.id)
    await db_session.commit()

    # Must NOT match because they belong to the same group!
    assert match is None

    # Both must remain in queue
    p1 = await PartyService.get_party_by_id(db_session, party1.id)
    p2 = await PartyService.get_party_by_id(db_session, party2.id)
    assert p1.status == PartyStatus.IN_QUEUE
    assert p2.status == PartyStatus.IN_QUEUE


@pytest.mark.asyncio
async def test_hard_compatibility_rules(db_session):
    """Parties with different games or modes must NEVER match."""
    g1 = await create_test_group(db_session, "گروه ۱")
    g2 = await create_test_group(db_session, "گروه ۲")
    u1 = await create_test_user(db_session, "A")
    u2 = await create_test_user(db_session, "B")

    # Different games: RPS vs High Card
    party_rps = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    party_hc = await PartyService.create_party(db_session, g2.id, u2.id, "high_card", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party_rps.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, party_hc.id, u2.id)
    await db_session.commit()

    match = await MatchmakingService.find_and_create_match(db_session, party_rps.id)
    assert match is None


@pytest.mark.asyncio
async def test_match_creation_and_acceptance_flow(db_session):
    """
    Complete flow:
    Find Match -> Match Found -> One Accept per side -> ACTIVE state.
    """
    g1 = await create_test_group(db_session, "گروه شاهین")
    g2 = await create_test_group(db_session, "گروه عقاب")
    u1 = await create_test_user(db_session, "شاهین ۱")
    u2 = await create_test_user(db_session, "عقاب ۱")

    party1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    party2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    # Enqueue party 1
    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await db_session.commit()

    # Enqueue party 2 and trigger match finding
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, party2.id)
    await db_session.commit()

    assert match is not None
    assert match.state == MatchState.WAITING_FOR_ACCEPTANCE
    assert match.group1_accepted is False
    assert match.group2_accepted is False
    assert len(match.players) == 2

    # First user accepts
    m, both1 = await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await db_session.commit()
    assert both1 is False
    assert (m.group1_accepted or m.group2_accepted) is True
    assert (m.group1_accepted and m.group2_accepted) is False
    assert m.state == MatchState.WAITING_FOR_ACCEPTANCE

    # Second user accepts
    m, both2 = await AcceptanceService.accept_match(db_session, match.id, u2.id)
    await db_session.commit()
    assert both2 is True
    assert m.group1_accepted is True
    assert m.group2_accepted is True
    assert m.state == MatchState.ACTIVE
    assert m.started_at is not None



@pytest.mark.asyncio
async def test_acceptance_timeout_cancels_match_and_releases_parties(db_session):
    """Expired acceptance deadline cancels the match and releases both parties."""
    from datetime import datetime, timezone, timedelta

    g1 = await create_test_group(db_session, "تیم timeout ۱")
    g2 = await create_test_group(db_session, "تیم timeout ۲")
    u1 = await create_test_user(db_session, "timeout ۱")
    u2 = await create_test_user(db_session, "timeout ۲")

    party1 = await PartyService.create_party(
        db_session, g1.id, u1.id, "connect_four", "solo"
    )
    party2 = await PartyService.create_party(
        db_session, g2.id, u2.id, "connect_four", "solo"
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(
        db_session, party2.id
    )
    await db_session.commit()

    assert match is not None
    assert match.state == MatchState.WAITING_FOR_ACCEPTANCE

    match.acceptance_deadline = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    await db_session.commit()

    result = await AcceptanceService.check_and_handle_acceptance_timeout(
        db_session, match.id
    )
    await db_session.commit()

    assert result is not None
    assert result.state == MatchState.CANCELLED

    p1 = await PartyService.get_party_by_id(db_session, party1.id)
    p2 = await PartyService.get_party_by_id(db_session, party2.id)

    assert p1.status == PartyStatus.FULL
    assert p2.status == PartyStatus.FULL


@pytest.mark.asyncio
async def test_acceptance_timeout_does_not_cancel_before_deadline(db_session):
    """A future acceptance deadline must leave the match and parties unchanged."""
    from datetime import datetime, timezone, timedelta

    g1 = await create_test_group(db_session, "تیم future ۱")
    g2 = await create_test_group(db_session, "تیم future ۲")
    u1 = await create_test_user(db_session, "future ۱")
    u2 = await create_test_user(db_session, "future ۲")

    party1 = await PartyService.create_party(
        db_session, g1.id, u1.id, "connect_four", "solo"
    )
    party2 = await PartyService.create_party(
        db_session, g2.id, u2.id, "connect_four", "solo"
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(
        db_session, party2.id
    )
    await db_session.commit()

    assert match is not None
    assert match.state == MatchState.WAITING_FOR_ACCEPTANCE

    match.acceptance_deadline = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    await db_session.commit()

    result = await AcceptanceService.check_and_handle_acceptance_timeout(
        db_session, match.id
    )
    await db_session.commit()

    assert result is None

    refreshed_match = await db_session.get(Match, match.id)
    assert refreshed_match.state == MatchState.WAITING_FOR_ACCEPTANCE

    p1 = await PartyService.get_party_by_id(db_session, party1.id)
    p2 = await PartyService.get_party_by_id(db_session, party2.id)

    assert p1.status == PartyStatus.IN_MATCH
    assert p2.status == PartyStatus.IN_MATCH

@pytest.mark.asyncio
async def test_match_rejection_before_gameplay(db_session):
    """If either side rejects before gameplay, match is cancelled with no result applied."""
    g1 = await create_test_group(db_session, "تیم قرمز")
    g2 = await create_test_group(db_session, "تیم آبی")
    u1 = await create_test_user(db_session, "قرمز ۱")
    u2 = await create_test_user(db_session, "آبی ۱")

    party1 = await PartyService.create_party(db_session, g1.id, u1.id, "connect_four", "solo")
    party2 = await PartyService.create_party(db_session, g2.id, u2.id, "connect_four", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, party2.id)
    await db_session.commit()

    # User 1 rejects
    rejected_match = await AcceptanceService.reject_match(db_session, match.id, u1.id)
    await db_session.commit()

    assert rejected_match.state == MatchState.CANCELLED

    # Both parties should be reverted to FULL so players can retry
    p1 = await PartyService.get_party_by_id(db_session, party1.id)
    p2 = await PartyService.get_party_by_id(db_session, party2.id)
    assert p1.status == PartyStatus.FULL
    assert p2.status == PartyStatus.FULL


@pytest.mark.asyncio
async def test_state_machine_illegal_transitions():
    """Verify illegal transitions are prevented by can_transition."""
    # Legal transitions
    assert AcceptanceService.can_transition(MatchState.CREATED, MatchState.WAITING_FOR_OPPONENT) is True
    assert AcceptanceService.can_transition(MatchState.WAITING_FOR_ACCEPTANCE, MatchState.ACTIVE) is True
    assert AcceptanceService.can_transition(MatchState.WAITING_FOR_ACCEPTANCE, MatchState.CANCELLED) is True
    assert AcceptanceService.can_transition(MatchState.ACTIVE, MatchState.FINISHED) is True

    # Illegal transitions
    assert AcceptanceService.can_transition(MatchState.CREATED, MatchState.FINISHED) is False
    assert AcceptanceService.can_transition(MatchState.WAITING_FOR_ACCEPTANCE, MatchState.FINISHED) is False
    assert AcceptanceService.can_transition(MatchState.FINISHED, MatchState.ACTIVE) is False
    assert AcceptanceService.can_transition(MatchState.CANCELLED, MatchState.ACTIVE) is False


@pytest.mark.asyncio
async def test_duplicate_queue_request_is_rejected(db_session):
    """The same party must never create or reactivate a duplicate queue entry."""
    group = await create_test_group(db_session, "گروه صف تکراری")
    user = await create_test_user(db_session, "بازیکن صف تکراری")

    party = await PartyService.create_party(
        db_session,
        group.id,
        user.id,
        "rock_paper_scissors",
        "solo",
    )
    await db_session.commit()

    # First enqueue must succeed.
    first_entry = await MatchmakingService.enqueue_party(
        db_session,
        party.id,
        user.id,
    )
    await db_session.commit()

    assert first_entry is not None

    # Second enqueue for the same party must be rejected.
    with pytest.raises(InvalidActionException):
        await MatchmakingService.enqueue_party(
            db_session,
            party.id,
            user.id,
        )

    # Verify the party is still in queue.
    refreshed_party = await PartyService.get_party_by_id(
        db_session,
        party.id,
    )
    assert refreshed_party.status == PartyStatus.IN_QUEUE

    # Verify there is exactly one active queue entry.
    result = await db_session.execute(
        select(MatchmakingQueue).where(
            MatchmakingQueue.party_id == party.id,
            MatchmakingQueue.is_active == True,
        )
    )
    active_entries = result.scalars().all()

    assert len(active_entries) == 1
    assert active_entries[0].id == first_entry.id


@pytest.mark.asyncio
async def test_party_cannot_be_matched_twice(db_session):
    """A party already moved into a match must not receive a second match."""
    g1 = await create_test_group(db_session, "گروه دوبل ۱")
    g2 = await create_test_group(db_session, "گروه دوبل ۲")
    u1 = await create_test_user(db_session, "بازیکن دوبل ۱")
    u2 = await create_test_user(db_session, "بازیکن دوبل ۲")

    party1 = await PartyService.create_party(
        db_session, g1.id, u1.id, "rock_paper_scissors", "solo"
    )
    party2 = await PartyService.create_party(
        db_session, g2.id, u2.id, "rock_paper_scissors", "solo"
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)

    match1 = await MatchmakingService.find_and_create_match(
        db_session, party1.id
    )
    await db_session.commit()

    assert match1 is not None

    # The same party is now IN_MATCH and its queue entry is inactive.
    p1 = await PartyService.get_party_by_id(db_session, party1.id)
    assert p1.status == PartyStatus.IN_MATCH

    # Trying to find another match for the same party must not create one.
    match2 = await MatchmakingService.find_and_create_match(
        db_session, party1.id
    )
    await db_session.commit()

    assert match2 is None

    # There must still be exactly one match involving this party.
    result = await db_session.execute(
        select(Match).where(
            (Match.party1_id == party1.id) | (Match.party2_id == party1.id)
        )
    )
    matches = result.scalars().all()

    assert len(matches) == 1
    assert matches[0].id == match1.id


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_matchmaking_does_not_create_duplicate_match(db_session):
    """Two concurrent match attempts must not create duplicate matches."""
    import asyncio
    import os
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    g1 = await create_test_group(db_session, "گروه ریس ۱")
    g2 = await create_test_group(db_session, "گروه ریس ۲")
    u1 = await create_test_user(db_session, "بازیکن ریس ۱")
    u2 = await create_test_user(db_session, "بازیکن ریس ۲")

    party1 = await PartyService.create_party(
        db_session, g1.id, u1.id, "rock_paper_scissors", "solo"
    )
    party2 = await PartyService.create_party(
        db_session, g2.id, u2.id, "rock_paper_scissors", "solo"
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    await db_session.commit()

    test_database_url = os.environ["sib_TEST_DATABASE_URL"]
    engine = create_async_engine(test_database_url, pool_size=5, max_overflow=0)
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def attempt(party_id):
        async with session_factory() as session:
            try:
                result = await MatchmakingService.find_and_create_match(
                    session, party_id
                )
                await session.commit()
                return ("ok", result.id if result else None)
            except Exception as exc:
                await session.rollback()
                return ("error", type(exc).__name__)

    try:
        results = await asyncio.gather(
            attempt(party1.id),
            attempt(party2.id),
        )

        # At most one attempt may successfully create a match.
        successful_matches = [
            result for status, result in results
            if status == "ok" and result is not None
        ]
        assert len(successful_matches) <= 1

        # Verify database state: exactly one match may exist.
        result = await db_session.execute(
            select(Match).where(
                ((Match.party1_id == party1.id) & (Match.party2_id == party2.id))
                | ((Match.party1_id == party2.id) & (Match.party2_id == party1.id))
            )
        )
        matches = result.scalars().all()

        assert len(matches) <= 1

        if matches:
            assert matches[0].state == MatchState.WAITING_FOR_ACCEPTANCE
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_matchmaking_releases_party_and_queue(db_session):
    """Cancelling matchmaking must release the party and deactivate its queue entry."""
    group = await create_test_group(db_session, "گروه لغو صف")
    user = await create_test_user(db_session, "بازیکن لغو صف")

    party = await PartyService.create_party(
        db_session,
        group.id,
        user.id,
        "rock_paper_scissors",
        "solo",
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(
        db_session,
        party.id,
        user.id,
    )
    await db_session.commit()

    await MatchmakingService.cancel_matchmaking(
        db_session,
        party.id,
        user.id,
    )
    await db_session.commit()

    refreshed_party = await PartyService.get_party_by_id(
        db_session,
        party.id,
    )
    assert refreshed_party.status == PartyStatus.FULL

    result = await db_session.execute(
        select(MatchmakingQueue).where(
            MatchmakingQueue.party_id == party.id,
            MatchmakingQueue.is_active == True,
        )
    )
    active_entries = result.scalars().all()
    assert len(active_entries) == 0

    # The released party must be able to queue again.
    new_entry = await MatchmakingService.enqueue_party(
        db_session,
        party.id,
        user.id,
    )
    await db_session.commit()

    assert new_entry is not None
    assert new_entry.is_active is True

    refreshed_party = await PartyService.get_party_by_id(
        db_session,
        party.id,
    )
    assert refreshed_party.status == PartyStatus.IN_QUEUE



@pytest.mark.asyncio
async def test_expired_queue_entry_is_cleaned_up(db_session):
    """Expired queue entries are deactivated and their parties released."""
    from datetime import datetime, timezone, timedelta
    from sib.database.models import MatchmakingQueue, PartyStatus
    from sib.services.maintenance import MaintenanceService

    group = await create_test_group(db_session, "گروه expired")
    user = await create_test_user(db_session, "بازیکن expired")

    party = await PartyService.create_party(
        db_session,
        group.id,
        user.id,
        "rock_paper_scissors",
        "solo",
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(
        db_session,
        party.id,
        user.id,
    )
    await db_session.commit()

    queue_entry = await db_session.scalar(
        select(MatchmakingQueue).where(
            MatchmakingQueue.party_id == party.id
        )
    )
    assert queue_entry is not None
    assert queue_entry.is_active is True

    # Simulate a genuinely expired queue entry.
    queue_entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    processed = await MaintenanceService.process_expired_queue_entries(
        db_session
    )

    assert processed == 1

    refreshed_queue = await db_session.scalar(
        select(MatchmakingQueue).where(
            MatchmakingQueue.party_id == party.id
        )
    )
    assert refreshed_queue is not None
    assert refreshed_queue.is_active is False

    refreshed_party = await PartyService.get_party_by_id(
        db_session,
        party.id,
    )
    assert refreshed_party is not None
    assert refreshed_party.status == PartyStatus.FULL

@pytest.mark.asyncio
async def test_active_orphan_queue_entry_is_not_silently_expired(db_session):
    """An unexpired queue entry for a non-queued party is currently left untouched."""
    from datetime import datetime, timezone, timedelta
    from sib.database.models import MatchmakingQueue, PartyStatus
    from sib.services.maintenance import MaintenanceService

    group = await create_test_group(db_session, "گروه orphan")
    user = await create_test_user(db_session, "بازیکن orphan")

    party = await PartyService.create_party(
        db_session,
        group.id,
        user.id,
        "rock_paper_scissors",
        "solo",
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(
        db_session,
        party.id,
        user.id,
    )
    await db_session.commit()

    queue_entry = await db_session.scalar(
        select(MatchmakingQueue).where(
            MatchmakingQueue.party_id == party.id
        )
    )
    assert queue_entry is not None

    # Simulate an orphaned queue row: party is no longer IN_QUEUE,
    # but the queue deadline has not expired.
    party.status = PartyStatus.FULL
    queue_entry.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db_session.commit()

    processed = await MaintenanceService.process_expired_queue_entries(
        db_session
    )

    assert processed == 0

    refreshed_queue = await db_session.scalar(
        select(MatchmakingQueue).where(
            MatchmakingQueue.party_id == party.id
        )
    )
    assert refreshed_queue is not None
    assert refreshed_queue.is_active is True


@pytest.mark.asyncio
async def test_turn_timeout_finishes_match_and_applies_result(db_session):
    """Expired turn deadline forfeits the current player and applies the result exactly once."""
    g1 = await create_test_group(db_session, "تیم turn timeout ۱")
    g2 = await create_test_group(db_session, "تیم turn timeout ۲")
    u1 = await create_test_user(db_session, "بازیکن timeout ۱")
    u2 = await create_test_user(db_session, "بازیکن timeout ۲")

    party1 = await PartyService.create_party(
        db_session, g1.id, u1.id, "rock_paper_scissors", "solo"
    )
    party2 = await PartyService.create_party(
        db_session, g2.id, u2.id, "rock_paper_scissors", "solo"
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party1.id, u1.id)
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, party2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, party2.id)
    await db_session.commit()

    assert match is not None
    assert len(match.players) == 2

    # Both sides accept -> ACTIVE.
    m, both1 = await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await db_session.commit()
    assert both1 is False

    m, both2 = await AcceptanceService.accept_match(db_session, match.id, u2.id)
    await db_session.commit()
    assert both2 is True
    assert m.state == MatchState.ACTIVE

    # Force a real expired turn belonging to player 1.
    m.current_turn_player_id = u1.id
    m.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    # Read group stats by the actual Match group slots, not the local g1/g2
    # variables, because matchmaking may assign either group to either slot.
    group1_stats = await db_session.scalar(
        select(GroupStats).where(GroupStats.group_id == match.group1_id)
    )
    group2_stats = await db_session.scalar(
        select(GroupStats).where(GroupStats.group_id == match.group2_id)
    )

    processed = await TimeoutService.process_turn_timeouts(db_session)

    assert match.id in processed

    await db_session.refresh(match)
    await db_session.refresh(group1_stats)
    await db_session.refresh(group2_stats)

    assert match.state == MatchState.FINISHED
    assert match.result_applied is True

    # The timed-out player is u1. Determine the actual team from MatchPlayer,
    # because matchmaking is free to assign group1/group2 ordering.
    timed_out_player = next(p for p in match.players if p.user_id == u1.id)
    assert timed_out_player.team in (1, 2)

    if timed_out_player.team == 1:
        assert group1_stats.matches_played == 1
        assert group1_stats.losses == 1
        assert group1_stats.wins == 0
        assert group2_stats.matches_played == 1
        assert group2_stats.wins == 1
    else:
        assert group1_stats.matches_played == 1
        assert group1_stats.wins == 1
        assert group2_stats.matches_played == 1
        assert group2_stats.losses == 1

    wins1_after = group1_stats.wins
    wins2_after = group2_stats.wins

    # Idempotency: a second timeout pass must not apply the result again.
    processed_again = await TimeoutService.process_turn_timeouts(db_session)

    assert match.id not in processed_again

    await db_session.refresh(group1_stats)
    await db_session.refresh(group2_stats)

    assert group1_stats.wins == wins1_after
    assert group2_stats.wins == wins2_after
    assert group1_stats.matches_played == 1
    assert group2_stats.matches_played == 1
