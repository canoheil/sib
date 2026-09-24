"""Tests for acceptance timeout and active turn timeout workers."""

from datetime import datetime, timedelta, timezone
import pytest
from sib.core.errors import MatchNotFoundException

from sib.database.models import (
    Match,
    MatchState,
    Party,
    PartyStatus,
)
from sib.services.timeout import TimeoutService
from sib.services.party import PartyService
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_acceptance_timeout_processing(db_session):
    """TimeoutService cancels matches in WAITING_FOR_ACCEPTANCE when acceptance_deadline passes."""
    g1 = await create_test_group(db_session, "گروه تایم‌اوت ۱")
    g2 = await create_test_group(db_session, "گروه تایم‌اوت ۲")
    u1 = await create_test_user(db_session, "کاربر ۱")
    u2 = await create_test_user(db_session, "کاربر ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "battleship", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "battleship", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)

    # Set deadline in past
    match.acceptance_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    cancelled_ids = await TimeoutService.process_acceptance_timeouts(db_session)
    assert match.id in cancelled_ids

    await db_session.refresh(match)
    assert match.state == MatchState.CANCELLED

    # Parties released back to FULL
    await db_session.refresh(p1)
    await db_session.refresh(p2)
    assert p1.status == PartyStatus.FULL
    assert p2.status == PartyStatus.FULL


@pytest.mark.asyncio
async def test_turn_timeout_awards_forfeit_win_and_updates_rating(db_session):
    """When a player's turn times out, the match is forfeited to the opponent and rating is updated."""
    g1 = await create_test_group(db_session, "گروه تایم نوبت ۱")
    g2 = await create_test_group(db_session, "گروه تایم نوبت ۲")
    u1 = await create_test_user(db_session, "کاربر نوبت ۱")
    u2 = await create_test_user(db_session, "کاربر نوبت ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "connect_four", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "connect_four", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    # Set active match with u1 as current player and deadline in the past
    match.state = MatchState.ACTIVE
    match.current_turn_player_id = u1.id
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db_session.commit()

    processed_ids = await TimeoutService.process_turn_timeouts(db_session)
    assert match.id in processed_ids

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.rating_applied is True

    # Check that ratings were changed
    from sqlalchemy import select
    from sib.database.models import Rating
    r1 = await db_session.scalar(select(Rating).where(Rating.group_id == match.group1_id))
    r2 = await db_session.scalar(select(Rating).where(Rating.group_id == match.group2_id))
    assert r1.matches_played == 1
    assert r2.matches_played == 1

@pytest.mark.asyncio
async def test_active_match_leaver_forfeits_and_updates_rating(db_session):
    """An active match leaver forfeits for their team and applies rating exactly once."""
    from sqlalchemy import select
    from sib.database.models import MatchPlayer, Rating
    from sib.services.match_lifecycle import MatchLifecycleService

    g1 = await create_test_group(db_session, "گروه لیور ۱")
    g2 = await create_test_group(db_session, "گروه لیور ۲")
    u1 = await create_test_user(db_session, "کاربر لیور ۱")
    u2 = await create_test_user(db_session, "کاربر لیور ۲")

    p1 = await PartyService.create_party(
        db_session, g1.id, u1.id, "connect_four", "solo"
    )
    p2 = await PartyService.create_party(
        db_session, g2.id, u2.id, "connect_four", "solo"
    )
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)

    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    match.state = MatchState.ACTIVE
    await db_session.commit()

    result = await MatchLifecycleService.forfeit_player(
        db_session, match.id, u1.id
    )

    assert result.id == match.id
    assert result.state == MatchState.FINISHED
    assert result.rating_applied is True
    leaver_team = await db_session.scalar(
        select(MatchPlayer.team)
        .where(MatchPlayer.match_id == match.id)
        .where(MatchPlayer.user_id == u1.id)
    )
    expected_winner_group = match.group2_id if leaver_team == 1 else match.group1_id
    assert result.winner_group_id == expected_winner_group

    leaver = await db_session.scalar(
        select(MatchPlayer)
        .where(MatchPlayer.match_id == match.id)
        .where(MatchPlayer.user_id == u1.id)
    )
    opponent = await db_session.scalar(
        select(MatchPlayer)
        .where(MatchPlayer.match_id == match.id)
        .where(MatchPlayer.user_id == u2.id)
    )

    assert leaver.is_active is False
    assert leaver.result == "LOSS"
    assert opponent.result == "WIN"

    r1 = await db_session.scalar(
        select(Rating).where(Rating.group_id == match.group1_id)
    )
    r2 = await db_session.scalar(
        select(Rating).where(Rating.group_id == match.group2_id)
    )

    assert r1.matches_played == 1
    assert r2.matches_played == 1

    first_delta_1 = r1.rating
    first_delta_2 = r2.rating

    # A second leave attempt must not apply another result/rating update.
    with pytest.raises(MatchNotFoundException):
        await MatchLifecycleService.forfeit_player(
            db_session, match.id, u1.id
        )

    await db_session.refresh(r1)
    await db_session.refresh(r2)

    assert r1.rating == first_delta_1
    assert r2.rating == first_delta_2
    assert r1.matches_played == 1
    assert r2.matches_played == 1
