"""Recovery tests verifying system restart resiliency and orphaned state cleanup."""

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from sib.database.models import (
    Match,
    MatchState,
    MatchmakingQueue,
    Party,
    PartyStatus,
)
from sib.services.recovery import RecoveryService
from sib.services.party import PartyService
from sib.matchmaking.matcher import MatchmakingService
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_recovery_cancels_expired_acceptance_matches(db_session):
    """Matches stuck in WAITING_FOR_ACCEPTANCE past deadline must be cancelled on restart."""
    g1 = await create_test_group(db_session, "گروه ریکاوری ۱")
    g2 = await create_test_group(db_session, "گروه ریکاوری ۲")
    u1 = await create_test_user(db_session, "کاربر ریکاوری ۱")
    u2 = await create_test_user(db_session, "کاربر ریکاوری ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)

    # Artificially expire acceptance deadline
    match.acceptance_deadline = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db_session.commit()

    # Run recovery
    stats = await RecoveryService.recover_on_startup(db_session)

    assert stats["expired_acceptance_matches"] >= 1

    await db_session.refresh(match)
    assert match.state == MatchState.CANCELLED

    # Parties should be restored to FULL
    await db_session.refresh(p1)
    await db_session.refresh(p2)
    assert p1.status == PartyStatus.FULL
    assert p2.status == PartyStatus.FULL


@pytest.mark.asyncio
async def test_recovery_clears_dangling_matchmaking_queue_entries(db_session):
    """Queue entries pointing to cancelled or non-queue parties must be purged on restart."""
    g = await create_test_group(db_session, "گروه صف سرگردان")
    u = await create_test_user(db_session, "کاربر صف")

    p = await PartyService.create_party(db_session, g.id, u.id, "tic_tac_toe", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p.id, u.id)
    await db_session.commit()

    # Manually change party status to CANCELLED to simulate abrupt termination
    p.status = PartyStatus.CANCELLED
    await db_session.commit()

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["cleared_queue_entries"] >= 1

    # Verify queue entry is gone
    remaining = (await db_session.scalars(select(MatchmakingQueue).where(MatchmakingQueue.party_id == p.id))).all()
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_recovery_finishes_interrupted_active_matches_past_turn_deadline(db_session):
    """Active matches that exceeded their turn deadline during downtime must be finished with rating applied."""
    g1 = await create_test_group(db_session, "گروه بازی قطع‌شده ۱")
    g2 = await create_test_group(db_session, "گروه بازی قطع‌شده ۲")
    u1 = await create_test_user(db_session, "کاربر قطع ۱")
    u2 = await create_test_user(db_session, "کاربر قطع ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "high_card", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "high_card", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)

    # Set match active but with expired turn deadline
    match.state = MatchState.ACTIVE
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db_session.commit()

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["expired_abandoned_matches"] >= 1

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
