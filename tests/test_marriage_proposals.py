"""Focused tests for MarriageProposalService (Phase 3 — Proposal Lifecycle)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sib.database import (
    Group,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
)
from sib.database.connection import get_async_sessionmaker
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.marriage_proposal import (
    InvalidProposalParticipantError,
    MarriageProposalService,
    NotProposalTargetError,
    ProposalError,
    ProposalExpiredError,
    ProposalInvalidReason,
    ProposalNotExpiredYetError,
    ProposalNotFoundError,
    ProposalNotPendingError,
    ProposerAlreadyMarriedError,
    ProposerHasPendingProposalError,
    SelfProposalError,
    TargetAlreadyMarriedError,
    WeddingRingRequiredError,
)


async def _mk_user(session, user_id: int, name: str = "کاربر", *, is_bot: bool = False) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=f"{name}{user_id}",
            language_code="fa",
            is_bot=is_bot,
        )
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = -1001001, title: str = "گروه سیب") -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title=title)
        session.add(group)
        await session.flush()
    return group


async def _grant_ring(session, user_id: int, quantity: int = 1) -> None:
    await ItemService.grant(session, user_id, "wedding_ring", quantity)
    await session.flush()


# ---------------------------------------------------------------------------
# 1. Creation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_success_and_ring_remains(db_session):
    proposer = await _mk_user(db_session, 1001, "پیشنهاددهنده")
    target = await _mk_user(db_session, 1002, "مخاطب")
    group = await _mk_group(db_session, -1002001)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session,
        proposer_id=proposer.id,
        target_id=target.id,
        group_id=group.id,
        message_id=777,
        now=now,
    )
    await db_session.commit()

    assert proposal.id is not None
    assert proposal.proposer_id == proposer.id
    assert proposal.target_id == target.id
    assert proposal.group_id == group.id
    assert proposal.message_id == 777
    assert proposal.status == ProposalStatus.PENDING
    assert proposal.is_pending is True
    assert proposal.is_terminal is False
    assert proposal.resolved_at is None
    assert proposal.invalid_reason is None
    # Lifetime from config: marriage_proposal_timeout_seconds = 300s
    expires_utc = (
        proposal.expires_at
        if proposal.expires_at.tzinfo
        else proposal.expires_at.replace(tzinfo=timezone.utc)
    )
    assert expires_utc == now + timedelta(seconds=300)

    # Structured display helpers work without lazy-loading errors
    assert proposal.proposer_display_name == proposer.display_name
    assert proposal.target_display_name == target.display_name
    assert proposal.proposal is proposal

    # Ring MUST remain in proposer's inventory after proposal creation
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_create_proposal_respects_configured_timeout(db_session):
    proposer = await _mk_user(db_session, 1011)
    target = await _mk_user(db_session, 1012)
    group = await _mk_group(db_session, -1002011)
    await _grant_ring(db_session, proposer.id, 1)
    await ConfigService.set(db_session, "marriage_proposal_timeout_seconds", 420)
    await db_session.commit()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session,
        proposer_id=proposer.id,
        target_id=target.id,
        group_id=group.id,
        message_id=888,
        now=now,
    )
    await db_session.commit()

    expires_utc = (
        proposal.expires_at
        if proposal.expires_at.tzinfo
        else proposal.expires_at.replace(tzinfo=timezone.utc)
    )
    assert expires_utc == now + timedelta(seconds=420)


@pytest.mark.asyncio
async def test_create_proposal_rejected_for_self_proposal(db_session):
    user = await _mk_user(db_session, 1003)
    group = await _mk_group(db_session, -1002002)
    await _grant_ring(db_session, user.id, 1)
    await db_session.commit()

    with pytest.raises(SelfProposalError):
        await MarriageProposalService.create_proposal(
            db_session,
            proposer_id=user.id,
            target_id=user.id,
            group_id=group.id,
            message_id=111,
        )


@pytest.mark.asyncio
async def test_create_proposal_rejected_for_bot_participant(db_session):
    human = await _mk_user(db_session, 1004)
    bot_user = await _mk_user(db_session, 1005, is_bot=True)
    group = await _mk_group(db_session, -1002003)
    await _grant_ring(db_session, human.id, 1)
    await db_session.commit()

    with pytest.raises(InvalidProposalParticipantError):
        await MarriageProposalService.create_proposal(
            db_session,
            proposer_id=human.id,
            target_id=bot_user.id,
            group_id=group.id,
            message_id=112,
        )


@pytest.mark.asyncio
async def test_create_proposal_rejected_without_wedding_ring(db_session):
    proposer = await _mk_user(db_session, 1006)
    target = await _mk_user(db_session, 1007)
    group = await _mk_group(db_session, -1002004)
    await db_session.commit()

    with pytest.raises(WeddingRingRequiredError):
        await MarriageProposalService.create_proposal(
            db_session,
            proposer_id=proposer.id,
            target_id=target.id,
            group_id=group.id,
            message_id=113,
        )


@pytest.mark.asyncio
async def test_create_proposal_rejected_when_proposer_already_married(db_session):
    proposer = await _mk_user(db_session, 1008)
    spouse = await _mk_user(db_session, 1009)
    target = await _mk_user(db_session, 1010)
    group = await _mk_group(db_session, -1002005)
    await _grant_ring(db_session, proposer.id, 1)

    db_session.add(
        Marriage(
            proposer_id=proposer.id,
            accepter_id=spouse.id,
            origin_group_id=group.id,
            status=MarriageStatus.ACTIVE,
        )
    )
    await db_session.commit()

    with pytest.raises(ProposerAlreadyMarriedError):
        await MarriageProposalService.create_proposal(
            db_session,
            proposer_id=proposer.id,
            target_id=target.id,
            group_id=group.id,
            message_id=114,
        )
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_create_proposal_rejected_when_target_already_married(db_session):
    proposer = await _mk_user(db_session, 1020)
    target = await _mk_user(db_session, 1021)
    target_spouse = await _mk_user(db_session, 1022)
    group = await _mk_group(db_session, -1002006)
    await _grant_ring(db_session, proposer.id, 1)

    db_session.add(
        Marriage(
            proposer_id=target.id,
            accepter_id=target_spouse.id,
            origin_group_id=group.id,
            status=MarriageStatus.ACTIVE,
        )
    )
    await db_session.commit()

    with pytest.raises(TargetAlreadyMarriedError):
        await MarriageProposalService.create_proposal(
            db_session,
            proposer_id=proposer.id,
            target_id=target.id,
            group_id=group.id,
            message_id=115,
        )
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_create_proposal_rejected_when_proposer_already_has_pending_outgoing(db_session):
    proposer = await _mk_user(db_session, 1030)
    target_b = await _mk_user(db_session, 1031)
    target_c = await _mk_user(db_session, 1032)
    group = await _mk_group(db_session, -1002007)
    await _grant_ring(db_session, proposer.id, 2)
    await db_session.commit()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    first = await MarriageProposalService.create_proposal(
        db_session,
        proposer_id=proposer.id,
        target_id=target_b.id,
        group_id=group.id,
        message_id=201,
        now=now,
    )
    await db_session.commit()

    with pytest.raises(ProposerHasPendingProposalError) as exc_info:
        await MarriageProposalService.create_proposal(
            db_session,
            proposer_id=proposer.id,
            target_id=target_c.id,
            group_id=group.id,
            message_id=202,
            now=now + timedelta(seconds=10),
        )
    assert exc_info.value.existing_proposal_id == first.id

    # Once the first proposal expires (after 300s), proposer CAN create a new proposal
    second = await MarriageProposalService.create_proposal(
        db_session,
        proposer_id=proposer.id,
        target_id=target_c.id,
        group_id=group.id,
        message_id=203,
        now=now + timedelta(seconds=301),
    )
    await db_session.commit()

    await db_session.refresh(first)
    assert first.status == ProposalStatus.EXPIRED
    assert second.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_multiple_users_can_propose_to_same_target(db_session):
    proposer_a = await _mk_user(db_session, 1040)
    proposer_b = await _mk_user(db_session, 1041)
    proposer_c = await _mk_user(db_session, 1042)
    target = await _mk_user(db_session, 1049)
    group = await _mk_group(db_session, -1002008)

    for uid in (proposer_a.id, proposer_b.id, proposer_c.id):
        await _grant_ring(db_session, uid, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    p1 = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=301, now=now
    )
    p2 = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target.id, group.id, message_id=302, now=now
    )
    p3 = await MarriageProposalService.create_proposal(
        db_session, proposer_c.id, target.id, group.id, message_id=303, now=now
    )
    await db_session.commit()

    incoming = await MarriageProposalService.get_pending_proposals_for_target(
        db_session, target.id, now=now
    )
    assert [p.id for p in incoming] == [p1.id, p2.id, p3.id]
    assert all(p.status == ProposalStatus.PENDING for p in incoming)


# ---------------------------------------------------------------------------
# 2. Rejection Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_can_reject_and_ring_remains(db_session):
    proposer = await _mk_user(db_session, 2001)
    target = await _mk_user(db_session, 2002)
    group = await _mk_group(db_session, -1003001)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 13, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=401, now=now
    )
    await db_session.commit()

    reject_time = now + timedelta(seconds=45)
    rejected = await MarriageProposalService.reject_proposal(
        db_session, proposal.id, target.id, now=reject_time
    )
    await db_session.commit()

    assert rejected.id == proposal.id
    assert rejected.status == ProposalStatus.REJECTED
    assert rejected.is_pending is False
    assert rejected.is_terminal is True
    resolved_utc = (
        rejected.resolved_at
        if rejected.resolved_at.tzinfo
        else rejected.resolved_at.replace(tzinfo=timezone.utc)
    )
    assert resolved_utc == reject_time

    # Proposer keeps their ring
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1
    # Outgoing slot is free again
    assert (
        await MarriageProposalService.get_active_outgoing_proposal(
            db_session, proposer.id, now=reject_time
        )
        is None
    )


@pytest.mark.asyncio
async def test_non_target_cannot_reject_proposal(db_session):
    proposer = await _mk_user(db_session, 2010)
    target = await _mk_user(db_session, 2011)
    stranger = await _mk_user(db_session, 2012)
    group = await _mk_group(db_session, -1003002)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=402
    )
    await db_session.commit()

    # Neither a third party nor the proposer themselves can call reject_proposal
    for invalid_actor in (stranger.id, proposer.id):
        with pytest.raises(NotProposalTargetError):
            await MarriageProposalService.reject_proposal(
                db_session, proposal.id, invalid_actor
            )

    await db_session.refresh(proposal)
    assert proposal.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_rejected_proposal_cannot_be_rejected_twice(db_session):
    proposer = await _mk_user(db_session, 2020)
    target = await _mk_user(db_session, 2021)
    group = await _mk_group(db_session, -1003003)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=403
    )
    await MarriageProposalService.reject_proposal(db_session, proposal.id, target.id)
    await db_session.commit()

    with pytest.raises(ProposalNotPendingError) as exc_info:
        await MarriageProposalService.reject_proposal(db_session, proposal.id, target.id)
    assert exc_info.value.current_status == ProposalStatus.REJECTED


# ---------------------------------------------------------------------------
# 3. Expiration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_proposal_can_expire_and_ring_remains(db_session):
    proposer = await _mk_user(db_session, 3001)
    target = await _mk_user(db_session, 3002)
    group = await _mk_group(db_session, -1004001)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 14, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=501, now=now
    )
    await db_session.commit()

    # Cannot expire before expires_at
    with pytest.raises(ProposalNotExpiredYetError):
        await MarriageProposalService.expire_proposal(
            db_session, proposal.id, now=now + timedelta(seconds=299)
        )

    # At expires_at (300s), expiration succeeds
    expire_time = now + timedelta(seconds=300)
    expired = await MarriageProposalService.expire_proposal(
        db_session, proposal.id, now=expire_time
    )
    await db_session.commit()

    assert expired.status == ProposalStatus.EXPIRED
    assert expired.is_terminal is True
    resolved_utc = (
        expired.resolved_at
        if expired.resolved_at.tzinfo
        else expired.resolved_at.replace(tzinfo=timezone.utc)
    )
    assert resolved_utc == expire_time
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_expired_proposal_cannot_be_rejected_or_invalidated_later(db_session):
    proposer = await _mk_user(db_session, 3010)
    target = await _mk_user(db_session, 3011)
    group = await _mk_group(db_session, -1004002)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 14, 30, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=502, now=now
    )
    await db_session.commit()

    # Attempting to reject after expiration transitions to EXPIRED and raises ProposalExpiredError
    late_time = now + timedelta(seconds=305)
    with pytest.raises(ProposalExpiredError):
        await MarriageProposalService.reject_proposal(
            db_session, proposal.id, target.id, now=late_time
        )
    await db_session.commit()

    refreshed = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert refreshed is not None
    assert refreshed.status == ProposalStatus.EXPIRED

    # Nor can an already-EXPIRED proposal be invalidated or expired again
    with pytest.raises(ProposalNotPendingError):
        await MarriageProposalService.invalidate_proposal(
            db_session, proposal.id, reason="late_invalidation"
        )
    with pytest.raises(ProposalNotPendingError):
        await MarriageProposalService.expire_proposal(
            db_session, proposal.id, now=late_time
        )


# ---------------------------------------------------------------------------
# 4. Invalidation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_proposal_can_be_invalidated_and_reason_persisted(db_session):
    proposer = await _mk_user(db_session, 4001)
    target = await _mk_user(db_session, 4002)
    group = await _mk_group(db_session, -1005001)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 15, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=601, now=now
    )
    await db_session.commit()

    inv_time = now + timedelta(seconds=60)
    invalidated = await MarriageProposalService.invalidate_proposal(
        db_session,
        proposal.id,
        reason=ProposalInvalidReason.TARGET_MARRIED,
        now=inv_time,
    )
    await db_session.commit()

    assert invalidated.status == ProposalStatus.INVALIDATED
    assert invalidated.invalid_reason == ProposalInvalidReason.TARGET_MARRIED
    resolved_utc = (
        invalidated.resolved_at
        if invalidated.resolved_at.tzinfo
        else invalidated.resolved_at.replace(tzinfo=timezone.utc)
    )
    assert resolved_utc == inv_time
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_terminal_proposal_cannot_be_invalidated(db_session):
    proposer = await _mk_user(db_session, 4010)
    target = await _mk_user(db_session, 4011)
    group = await _mk_group(db_session, -1005002)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=602
    )
    await MarriageProposalService.reject_proposal(db_session, proposal.id, target.id)
    await db_session.commit()

    with pytest.raises(ProposalNotPendingError) as exc_info:
        await MarriageProposalService.invalidate_proposal(
            db_session, proposal.id, reason=ProposalInvalidReason.TARGET_MARRIED
        )
    assert exc_info.value.current_status == ProposalStatus.REJECTED


# ---------------------------------------------------------------------------
# 5. Concurrency & Race Condition Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_only_one_terminal_resolution_wins(db_session):
    """Race C & D: simultaneous reject / invalidate / expire on the same proposal
    must result in exactly ONE winning terminal state transition."""
    proposer = await _mk_user(db_session, 5001)
    target = await _mk_user(db_session, 5002)
    group = await _mk_group(db_session, -1006001)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 16, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=701, now=now
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def try_reject():
        async with maker() as s:
            try:
                res = await MarriageProposalService.reject_proposal(
                    s, proposal.id, target.id, now=now + timedelta(seconds=10)
                )
                await s.commit()
                return ("REJECTED", res.status)
            except ProposalError:
                await s.rollback()
                return None

    async def try_invalidate():
        async with maker() as s:
            try:
                res = await MarriageProposalService.invalidate_proposal(
                    s,
                    proposal.id,
                    reason=ProposalInvalidReason.TARGET_MARRIED,
                    now=now + timedelta(seconds=10),
                )
                await s.commit()
                return ("INVALIDATED", res.status)
            except ProposalError:
                await s.rollback()
                return None

    results = await asyncio.gather(try_reject(), try_reject(), try_invalidate())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1

    db_session.expunge_all()
    final = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert final is not None
    assert final.status == winners[0][0]


@pytest.mark.asyncio
async def test_race_concurrent_outgoing_proposals_from_same_proposer(db_session):
    """Race E: Proposer attempts to create A -> B and A -> C simultaneously.
    Only ONE PENDING outgoing proposal may survive."""
    proposer = await _mk_user(db_session, 5010)
    target_b = await _mk_user(db_session, 5011)
    target_c = await _mk_user(db_session, 5012)
    group = await _mk_group(db_session, -1006002)
    await _grant_ring(db_session, proposer.id, 2)
    await db_session.commit()

    maker = get_async_sessionmaker()
    now = datetime(2026, 10, 3, 16, 30, 0, tzinfo=timezone.utc)

    async def propose_to(target_uid: int, msg_id: int):
        async with maker() as s:
            try:
                p = await MarriageProposalService.create_proposal(
                    s,
                    proposer_id=proposer.id,
                    target_id=target_uid,
                    group_id=group.id,
                    message_id=msg_id,
                    now=now,
                )
                await s.commit()
                return p.id
            except ProposalError:
                await s.rollback()
                return None

    results = await asyncio.gather(
        propose_to(target_b.id, 801),
        propose_to(target_c.id, 802),
    )
    created_ids = [pid for pid in results if pid is not None]
    assert len(created_ids) == 1

    db_session.expunge_all()
    pending_rows = (
        await db_session.execute(
            select(MarriageProposal).where(
                MarriageProposal.proposer_id == proposer.id,
                MarriageProposal.status == ProposalStatus.PENDING,
            )
        )
    ).scalars().all()
    assert len(pending_rows) == 1
    assert pending_rows[0].id == created_ids[0]


@pytest.mark.asyncio
async def test_resolving_one_proposal_for_target_invalidates_other_pending_proposals(db_session):
    """Race B: Multiple proposers propose to the same target; when one proposal wins,
    all other pending proposals for those users are safely invalidated while already
    terminal proposals remain untouched."""
    proposer_a = await _mk_user(db_session, 5020)
    proposer_b = await _mk_user(db_session, 5021)
    proposer_c = await _mk_user(db_session, 5022)
    target = await _mk_user(db_session, 5029)
    group = await _mk_group(db_session, -1006003)

    for uid in (proposer_a.id, proposer_b.id, proposer_c.id):
        await _grant_ring(db_session, uid, 1)
    await db_session.commit()

    now = datetime(2026, 10, 3, 17, 0, 0, tzinfo=timezone.utc)
    prop_a = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=901, now=now
    )
    prop_b = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target.id, group.id, message_id=902, now=now
    )
    prop_c = await MarriageProposalService.create_proposal(
        db_session, proposer_c.id, target.id, group.id, message_id=903, now=now
    )
    # Reject C's proposal first so it is already terminal (REJECTED)
    await MarriageProposalService.reject_proposal(
        db_session, prop_c.id, target.id, now=now + timedelta(seconds=10)
    )
    await db_session.commit()

    # Now A's proposal is accepted (simulated winning proposal) and we invalidate
    # all other pending proposals for the newly married pair (proposer_a, target).
    invalidated = await MarriageProposalService.invalidate_pending_for_users(
        db_session,
        (proposer_a.id, target.id),
        reason=ProposalInvalidReason.OTHER_PROPOSAL_ACCEPTED,
        exclude_proposal_id=prop_a.id,
        now=now + timedelta(seconds=20),
    )
    await db_session.commit()

    assert [p.id for p in invalidated] == [prop_b.id]
    assert invalidated[0].status == ProposalStatus.INVALIDATED
    assert invalidated[0].invalid_reason == ProposalInvalidReason.OTHER_PROPOSAL_ACCEPTED

    # C's already-REJECTED proposal was not overwritten
    refreshed_c = await MarriageProposalService.get_proposal(db_session, prop_c.id)
    assert refreshed_c is not None
    assert refreshed_c.status == ProposalStatus.REJECTED

    # A's winning proposal was excluded from invalidation
    refreshed_a = await MarriageProposalService.get_proposal(db_session, prop_a.id)
    assert refreshed_a is not None
    assert refreshed_a.status == ProposalStatus.PENDING

    # Proposer B kept their ring and can propose to someone else now
    assert await ItemService.get_total(db_session, proposer_b.id, "wedding_ring") == 1
    assert (
        await MarriageProposalService.get_active_outgoing_proposal(
            db_session, proposer_b.id, now=now + timedelta(seconds=25)
        )
        is None
    )


@pytest.mark.asyncio
async def test_expire_overdue_proposals_and_get_pending_proposal_queries(db_session):
    proposer_a = await _mk_user(db_session, 6001)
    proposer_b = await _mk_user(db_session, 6002)
    target = await _mk_user(db_session, 6009)
    group = await _mk_group(db_session, -1007001)
    await _grant_ring(db_session, proposer_a.id, 1)
    await _grant_ring(db_session, proposer_b.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 18, 0, 0, tzinfo=timezone.utc)
    early = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=1001, now=t0
    )
    later = await MarriageProposalService.create_proposal(
        db_session,
        proposer_b.id,
        target.id,
        group.id,
        message_id=1002,
        now=t0 + timedelta(seconds=120),
    )
    await db_session.commit()

    # At t0 + 305s: `early` is overdue, `later` is still active
    check_time = t0 + timedelta(seconds=305)
    assert (
        await MarriageProposalService.get_pending_proposal(
            db_session, later.id, now=check_time
        )
        is not None
    )

    swept = await MarriageProposalService.expire_overdue_proposals(
        db_session, now=check_time
    )
    await db_session.commit()

    assert [p.id for p in swept] == [early.id]
    assert swept[0].status == ProposalStatus.EXPIRED
    assert (
        await MarriageProposalService.get_pending_proposal(
            db_session, early.id, now=check_time
        )
        is None
    )
    # `later` remains PENDING
    remaining = await MarriageProposalService.get_pending_proposals_for_target(
        db_session, target.id, now=check_time
    )
    assert [p.id for p in remaining] == [later.id]


@pytest.mark.asyncio
async def test_race_expire_vs_reject_only_one_terminal_transition_wins(db_session):
    """Race C: A proposal expires while another transaction attempts to resolve it.
    Only one terminal transition may win."""
    proposer = await _mk_user(db_session, 6010)
    target = await _mk_user(db_session, 6011)
    group = await _mk_group(db_session, -1007002)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 19, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=1003, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def run_expire():
        async with maker() as s:
            try:
                res = await MarriageProposalService.expire_proposal(
                    s, proposal.id, now=t0 + timedelta(seconds=300)
                )
                await s.commit()
                return res.status
            except ProposalError:
                await s.rollback()
                return None

    async def run_reject():
        async with maker() as s:
            try:
                res = await MarriageProposalService.reject_proposal(
                    s, proposal.id, target.id, now=t0 + timedelta(seconds=299)
                )
                await s.commit()
                return res.status
            except ProposalError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(run_expire(), run_reject())
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    assert winners[0] in (ProposalStatus.EXPIRED, ProposalStatus.REJECTED)

    db_session.expunge_all()
    final = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert final is not None
    assert final.status == winners[0]
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1

