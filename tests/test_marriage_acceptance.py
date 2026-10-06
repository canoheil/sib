"""Focused tests for Phase 4 — Marriage Acceptance + Marriage Creation."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

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
from sib.services.player_xp import PlayerXPService
from sib.services.items import ItemService
from sib.services.marriage import (
    MarriageAcceptanceRaceLostError,
    MarriageAcceptanceResult,
    MarriageAlreadyExistsError,
    MarriageCooldownActiveError,
    MarriageError,
    MarriageNotFoundError,
    MarriageParticipantAlreadyMarriedError,
    MarriageService,
    NotProposalTargetError,
    ProposalExpiredError,
    ProposalNotAcceptableError,
    ProposalNotFoundError,
    ProposerAlreadyMarriedOnAcceptError,
    TargetAlreadyMarriedOnAcceptError,
    WeddingRingMissingAtAcceptanceError,
)
from sib.services.marriage_proposal import (
    MarriageProposalService,
    ProposalError,
    ProposalInvalidReason,
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


async def _mk_group(session, group_id: int = -1008001, title: str = "گروه ازدواج") -> Group:
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
# 1. Basic Acceptance & Marriage Creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_acceptance_creates_marriage_with_canonical_ids(db_session):
    # Proposer ID (7050) > Accepter ID (7010) to verify canonical user1_id < user2_id
    proposer = await _mk_user(db_session, 7050, "پیشنهاددهنده")
    target = await _mk_user(db_session, 7010, "مخاطب")
    group = await _mk_group(db_session, -1008100)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session,
        proposer_id=proposer.id,
        target_id=target.id,
        group_id=group.id,
        message_id=5001,
        now=t0,
    )
    await db_session.commit()
    assert (await PlayerXPService.get_progress(db_session, proposer.id)).total_xp == 0
    assert (await PlayerXPService.get_progress(db_session, target.id)).total_xp == 0

    accept_time = t0 + timedelta(seconds=40)
    result = await MarriageService.accept_proposal(
        db_session,
        proposal_id=proposal.id,
        actor_user_id=target.id,
        now=accept_time,
    )
    await db_session.commit()

    assert isinstance(result, MarriageAcceptanceResult)
    assert result.marriage.id is not None
    assert result.user1_id == 7010
    assert result.user2_id == 7050
    assert result.proposer_id == 7050
    assert result.accepter_id == 7010
    assert result.origin_group_id == group.id
    assert result.proposal_id == proposal.id
    assert result.status == MarriageStatus.ACTIVE
    assert result.love_score == 0
    assert result.proposer.id == proposer.id
    assert result.accepter.id == target.id
    assert result.other_proposals_invalidated is False
    assert result.invalidated_proposals == ()
    assert (await PlayerXPService.get_progress(db_session, proposer.id)).total_xp == 50
    assert (await PlayerXPService.get_progress(db_session, target.id)).total_xp == 50

    married_utc = (
        result.married_at
        if result.married_at.tzinfo
        else result.married_at.replace(tzinfo=timezone.utc)
    )
    assert married_utc == accept_time

    # Winning proposal state
    assert result.winning_proposal.status == ProposalStatus.ACCEPTED
    resolved_utc = (
        result.winning_proposal.resolved_at
        if result.winning_proposal.resolved_at.tzinfo
        else result.winning_proposal.resolved_at.replace(tzinfo=timezone.utc)
    )
    assert resolved_utc == accept_time

    # Both users are now married globally
    assert await MarriageService.is_user_married(db_session, proposer.id) is True
    assert await MarriageService.is_user_married(db_session, target.id) is True


@pytest.mark.asyncio
async def test_acceptance_uses_configured_love_initial(db_session):
    proposer = await _mk_user(db_session, 7060)
    target = await _mk_user(db_session, 7061)
    group = await _mk_group(db_session, -1008101)
    await _grant_ring(db_session, proposer.id, 1)
    await ConfigService.set(db_session, "love_initial", 15)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5002
    )
    await db_session.commit()

    result = await MarriageService.accept_proposal(
        db_session, proposal.id, target.id
    )
    await db_session.commit()

    assert result.marriage.love_score == 15


@pytest.mark.asyncio
async def test_proposer_and_non_target_cannot_accept_proposal(db_session):
    proposer = await _mk_user(db_session, 7070)
    target = await _mk_user(db_session, 7071)
    stranger = await _mk_user(db_session, 7072)
    group = await _mk_group(db_session, -1008102)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5003
    )
    await db_session.commit()

    # Proposer cannot accept their own proposal
    with pytest.raises(NotProposalTargetError):
        await MarriageService.accept_proposal(
            db_session, proposal.id, proposer.id
        )

    # Third party cannot accept the proposal
    with pytest.raises(NotProposalTargetError):
        await MarriageService.accept_proposal(
            db_session, proposal.id, stranger.id
        )

    # Proposal remains PENDING, ring remains untouched, no Marriage created
    refreshed = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert refreshed is not None and refreshed.status == ProposalStatus.PENDING
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1
    assert await MarriageService.is_user_married(db_session, proposer.id) is False


# ---------------------------------------------------------------------------
# 2. Ring Ownership & Consumption Behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_consumed_exactly_once_on_successful_acceptance(db_session):
    proposer = await _mk_user(db_session, 7101)
    target = await _mk_user(db_session, 7102)
    group = await _mk_group(db_session, -1008201)
    await _grant_ring(db_session, proposer.id, 2)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5101
    )
    await db_session.commit()

    # Ring not consumed at creation
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 2

    await MarriageService.accept_proposal(db_session, proposal.id, target.id)
    await db_session.commit()

    # Exactly 1 ring consumed on acceptance (2 -> 1)
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_acceptance_fails_when_ring_was_sold_after_proposal_creation(db_session):
    proposer = await _mk_user(db_session, 7110)
    target = await _mk_user(db_session, 7111)
    group = await _mk_group(db_session, -1008202)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5102
    )
    await db_session.commit()

    # Proposer sells/consumes their only ring while proposal is PENDING
    await ItemService.consume(db_session, proposer.id, "wedding_ring", 1)
    await db_session.commit()
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 0

    with pytest.raises(WeddingRingMissingAtAcceptanceError) as exc_info:
        await MarriageService.accept_proposal(db_session, proposal.id, target.id)
    await db_session.commit()

    err = exc_info.value
    assert err.proposal_id == proposal.id
    assert err.proposer_id == proposer.id
    assert err.target_id == target.id
    assert err.group_id == group.id

    # No Marriage created, proposal NOT marked ACCEPTED
    assert await MarriageService.is_user_married(db_session, proposer.id) is False
    assert await MarriageService.is_user_married(db_session, target.id) is False
    refreshed = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert refreshed is not None
    assert refreshed.status != ProposalStatus.ACCEPTED


# ---------------------------------------------------------------------------
# 3. Marriage Eligibility & Divorce Cooldowns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_fails_if_proposer_or_target_already_married(db_session):
    proposer_a = await _mk_user(db_session, 7201)
    proposer_b = await _mk_user(db_session, 7202)
    target = await _mk_user(db_session, 7203)
    other_spouse = await _mk_user(db_session, 7204)
    group = await _mk_group(db_session, -1008301)
    await _grant_ring(db_session, proposer_a.id, 1)
    await _grant_ring(db_session, proposer_b.id, 1)
    await db_session.commit()

    prop_a = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=5201
    )
    prop_b = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target.id, group.id, message_id=5202
    )
    await db_session.commit()

    # Case 1: Proposer A becomes married to other_spouse before target accepts prop_a
    db_session.add(
        Marriage(
            proposer_id=proposer_a.id,
            accepter_id=other_spouse.id,
            origin_group_id=group.id,
            status=MarriageStatus.ACTIVE,
        )
    )
    await db_session.commit()

    with pytest.raises(MarriageParticipantAlreadyMarriedError) as exc_info:
        await MarriageService.accept_proposal(db_session, prop_a.id, target.id)
    assert isinstance(exc_info.value, ProposerAlreadyMarriedOnAcceptError)
    assert exc_info.value.user_id == proposer_a.id
    assert await ItemService.get_total(db_session, proposer_a.id, "wedding_ring") == 1

    # Case 2: Target becomes married to someone else before accepting prop_b
    third_spouse = await _mk_user(db_session, 7205)
    db_session.add(
        Marriage(
            proposer_id=target.id,
            accepter_id=third_spouse.id,
            origin_group_id=group.id,
            status=MarriageStatus.ACTIVE,
        )
    )
    await db_session.commit()

    with pytest.raises(MarriageParticipantAlreadyMarriedError) as exc_info2:
        await MarriageService.accept_proposal(db_session, prop_b.id, target.id)
    assert isinstance(exc_info2.value, TargetAlreadyMarriedOnAcceptError)
    assert exc_info2.value.user_id == target.id
    assert await ItemService.get_total(db_session, proposer_b.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_acceptance_enforces_proposer_and_target_divorce_cooldown(db_session):
    proposer = await _mk_user(db_session, 7210)
    target = await _mk_user(db_session, 7211)
    ex_spouse_1 = await _mk_user(db_session, 7212)
    ex_spouse_2 = await _mk_user(db_session, 7213)
    group = await _mk_group(db_session, -1008302)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5203, now=t0
    )

    # Proposer has a DIVORCED marriage with active cooldown until t0 + 1h
    divorced_proposer = Marriage(
        proposer_id=proposer.id,
        accepter_id=ex_spouse_1.id,
        origin_group_id=group.id,
        status=MarriageStatus.DIVORCED,
        divorced_at=t0 - timedelta(hours=23),
        cooldown_until=t0 + timedelta(hours=1),
    )
    db_session.add(divorced_proposer)
    await db_session.commit()

    with pytest.raises(MarriageCooldownActiveError) as exc_info:
        await MarriageService.accept_proposal(
            db_session, proposal.id, target.id, now=t0 + timedelta(seconds=30)
        )
    assert exc_info.value.user_id == proposer.id
    assert exc_info.value.role == "proposer"
    assert exc_info.value.remaining_seconds > 0
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1

    # Clear proposer's cooldown, set active cooldown on target
    divorced_proposer.cooldown_until = t0 - timedelta(seconds=1)
    divorced_target = Marriage(
        proposer_id=target.id,
        accepter_id=ex_spouse_2.id,
        origin_group_id=group.id,
        status=MarriageStatus.DIVORCED,
        divorced_at=t0 - timedelta(hours=23),
        cooldown_until=t0 + timedelta(minutes=2),
    )
    db_session.add(divorced_target)
    await db_session.commit()

    with pytest.raises(MarriageCooldownActiveError) as exc_info2:
        await MarriageService.accept_proposal(
            db_session, proposal.id, target.id, now=t0 + timedelta(seconds=60)
        )
    assert exc_info2.value.user_id == target.id
    assert exc_info2.value.role == "target"
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1

    # Once target's cooldown has expired (at t0 + 3 minutes, still within 5m proposal window),
    # acceptance succeeds!
    result = await MarriageService.accept_proposal(
        db_session, proposal.id, target.id, now=t0 + timedelta(minutes=3)
    )
    await db_session.commit()

    assert result.marriage.status == MarriageStatus.ACTIVE
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 0


# ---------------------------------------------------------------------------
# 4. Proposal State Transitions & Invalidation of Other Pending Proposals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_transitions_winning_proposal_and_invalidates_others(db_session):
    proposer_a = await _mk_user(db_session, 7301)
    proposer_b = await _mk_user(db_session, 7302)
    proposer_c = await _mk_user(db_session, 7303)
    target = await _mk_user(db_session, 7309)
    group = await _mk_group(db_session, -1008401)

    for uid in (proposer_a.id, proposer_b.id, proposer_c.id, target.id):
        await _grant_ring(db_session, uid, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 14, 0, 0, tzinfo=timezone.utc)
    winning = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=5301, now=t0
    )
    other_in_1 = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target.id, group.id, message_id=5302, now=t0
    )
    other_in_2 = await MarriageProposalService.create_proposal(
        db_session, proposer_c.id, target.id, group.id, message_id=5303, now=t0
    )
    # Also target had an outgoing pending proposal to proposer_b
    target_outgoing = await MarriageProposalService.create_proposal(
        db_session, target.id, proposer_b.id, group.id, message_id=5304, now=t0
    )
    await db_session.commit()

    accept_time = t0 + timedelta(seconds=30)
    result = await MarriageService.accept_proposal(
        db_session, winning.id, target.id, now=accept_time
    )
    await db_session.commit()

    assert result.winning_proposal.id == winning.id
    assert result.winning_proposal.status == ProposalStatus.ACCEPTED
    assert result.other_proposals_invalidated is True
    invalidated_ids = {p.id for p in result.invalidated_proposals}
    assert invalidated_ids == {other_in_1.id, other_in_2.id, target_outgoing.id}
    assert winning.id not in invalidated_ids

    for inv_id in invalidated_ids:
        row = await MarriageProposalService.get_proposal(db_session, inv_id)
        assert row is not None
        assert row.status == ProposalStatus.INVALIDATED
        assert row.invalid_reason == ProposalInvalidReason.OTHER_PROPOSAL_ACCEPTED
        assert row.resolved_at is not None

    # Only proposer_a's ring was consumed; proposer_b, proposer_c, and target kept theirs
    assert await ItemService.get_total(db_session, proposer_a.id, "wedding_ring") == 0
    assert await ItemService.get_total(db_session, proposer_b.id, "wedding_ring") == 1
    assert await ItemService.get_total(db_session, proposer_c.id, "wedding_ring") == 1
    assert await ItemService.get_total(db_session, target.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_expired_proposal_at_acceptance_marks_expired_without_marriage_or_ring_loss(db_session):
    proposer = await _mk_user(db_session, 7320)
    target = await _mk_user(db_session, 7321)
    group = await _mk_group(db_session, -1008402)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 14, 30, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5305, now=t0
    )
    await db_session.commit()

    with pytest.raises(ProposalExpiredError):
        await MarriageService.accept_proposal(
            db_session, proposal.id, target.id, now=t0 + timedelta(seconds=301)
        )
    await db_session.commit()

    refreshed = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert refreshed is not None
    assert refreshed.status == ProposalStatus.EXPIRED
    assert await MarriageService.is_user_married(db_session, proposer.id) is False
    assert await MarriageService.is_user_married(db_session, target.id) is False
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


# ---------------------------------------------------------------------------
# 5. Atomicity Under Simulated Step Failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_method",
    [
        "_consume_wedding_ring",
        "_create_marriage_record",
        "_transition_proposal_to_accepted",
        "_invalidate_conflicting_proposals",
    ],
)
async def test_atomicity_rollback_on_step_failure_leaves_no_partial_state(
    db_session, monkeypatch, failing_method
):
    proposer_a = await _mk_user(db_session, 7401)
    proposer_b = await _mk_user(db_session, 7402)
    target = await _mk_user(db_session, 7403)
    group = await _mk_group(db_session, -1008501)
    await _grant_ring(db_session, proposer_a.id, 1)
    await _grant_ring(db_session, proposer_b.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 15, 0, 0, tzinfo=timezone.utc)
    winning = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=5401, now=t0
    )
    competing = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target.id, group.id, message_id=5402, now=t0
    )
    await db_session.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError(f"Simulated failure in {failing_method}")

    monkeypatch.setattr(MarriageService, failing_method, _boom)

    with pytest.raises(RuntimeError, match=failing_method):
        await MarriageService.accept_proposal(
            db_session, winning.id, target.id, now=t0 + timedelta(seconds=15)
        )
    await db_session.commit()

    # Verify zero partial state:
    # 1. No Marriage row created
    assert await MarriageService.is_user_married(db_session, proposer_a.id) is False
    assert await MarriageService.is_user_married(db_session, target.id) is False
    # 2. Ring was NOT consumed
    assert await ItemService.get_total(db_session, proposer_a.id, "wedding_ring") == 1
    # 3. Winning proposal is still PENDING
    refreshed_win = await MarriageProposalService.get_proposal(db_session, winning.id)
    assert refreshed_win is not None and refreshed_win.status == ProposalStatus.PENDING
    # 4. Competing proposal is still PENDING
    refreshed_comp = await MarriageProposalService.get_proposal(
        db_session, competing.id
    )
    assert refreshed_comp is not None and refreshed_comp.status == ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# 6. Concurrency & Race Condition Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_two_proposals_to_same_target_accepted_concurrently(db_session):
    """Race A: A -> C and B -> C are both accepted concurrently.
    Exactly ONE Marriage may be created, ONE proposal ACCEPTED, the other INVALIDATED,
    and only the winning proposer's Ring is consumed."""
    proposer_a = await _mk_user(db_session, 7501)
    proposer_b = await _mk_user(db_session, 7502)
    target_c = await _mk_user(db_session, 7503)
    group = await _mk_group(db_session, -1008601)
    await _grant_ring(db_session, proposer_a.id, 1)
    await _grant_ring(db_session, proposer_b.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 16, 0, 0, tzinfo=timezone.utc)
    prop_a = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target_c.id, group.id, message_id=5501, now=t0
    )
    prop_b = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target_c.id, group.id, message_id=5502, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def try_accept(pid: int):
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, pid, target_c.id, now=t0 + timedelta(seconds=20)
                )
                await s.commit()
                return res.proposal_id
            except ProposalError as exc:
                await s.rollback()
                return exc

    outcomes = await asyncio.gather(
        try_accept(prop_a.id),
        try_accept(prop_b.id),
    )
    winners = [o for o in outcomes if isinstance(o, int)]
    losers = [o for o in outcomes if isinstance(o, ProposalError)]

    assert len(winners) == 1
    assert len(losers) == 1

    db_session.expunge_all()
    active_marriages = (
        await db_session.execute(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).scalars().all()
    assert len(active_marriages) == 1

    p_a = await MarriageProposalService.get_proposal(db_session, prop_a.id)
    p_b = await MarriageProposalService.get_proposal(db_session, prop_b.id)
    assert p_a is not None and p_b is not None
    assert {p_a.status, p_b.status} == {
        ProposalStatus.ACCEPTED,
        ProposalStatus.INVALIDATED,
    }

    # Exactly 1 ring consumed across both proposers
    rings_a = await ItemService.get_total(db_session, proposer_a.id, "wedding_ring")
    rings_b = await ItemService.get_total(db_session, proposer_b.id, "wedding_ring")
    assert rings_a + rings_b == 1


@pytest.mark.asyncio
async def test_race_same_proposal_accepted_concurrently(db_session):
    """Race B: Two requests attempt to accept the same proposal concurrently.
    Exactly one succeeds; the second receives a structured domain error; Ring consumed once."""
    proposer = await _mk_user(db_session, 7510)
    target = await _mk_user(db_session, 7511)
    group = await _mk_group(db_session, -1008602)
    # Give proposer 2 rings so a broken double-accept would consume 2 rings
    await _grant_ring(db_session, proposer.id, 2)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 16, 30, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5503, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def accept_once():
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, proposal.id, target.id, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return res.marriage_id
            except ProposalError as exc:
                await s.rollback()
                return exc

    outcomes = await asyncio.gather(accept_once(), accept_once())
    winners = [o for o in outcomes if isinstance(o, int)]
    errors = [o for o in outcomes if isinstance(o, ProposalError)]

    assert len(winners) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ProposalNotAcceptableError)

    db_session.expunge_all()
    count_marriages = await db_session.scalar(
        select(func.count(Marriage.id)).where(Marriage.status == MarriageStatus.ACTIVE)
    )
    assert count_marriages == 1
    # Ring consumed once: 2 -> 1
    assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_race_acceptance_versus_expiration(db_session):
    """Race D: Acceptance races against expiration.
    Exactly one terminal state wins; no Marriage is ever created from an expired proposal."""
    proposer = await _mk_user(db_session, 7520)
    target = await _mk_user(db_session, 7521)
    group = await _mk_group(db_session, -1008603)
    await _grant_ring(db_session, proposer.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 17, 0, 0, tzinfo=timezone.utc)
    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer.id, target.id, group.id, message_id=5504, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def do_accept():
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, proposal.id, target.id, now=t0 + timedelta(seconds=299)
                )
                await s.commit()
                return ("ACCEPTED", res.marriage_id)
            except ProposalError:
                await s.rollback()
                return None

    async def do_expire():
        async with maker() as s:
            try:
                res = await MarriageProposalService.expire_proposal(
                    s, proposal.id, now=t0 + timedelta(seconds=300)
                )
                await s.commit()
                return ("EXPIRED", res.id)
            except ProposalError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_accept(), do_expire())
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    winning_state = winners[0][0]

    db_session.expunge_all()
    final_prop = await MarriageProposalService.get_proposal(db_session, proposal.id)
    assert final_prop is not None
    assert final_prop.status == winning_state

    if winning_state == ProposalStatus.EXPIRED:
        assert await MarriageService.is_user_married(db_session, proposer.id) is False
        assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 1
    else:
        assert await MarriageService.is_user_married(db_session, proposer.id) is True
        assert await ItemService.get_total(db_session, proposer.id, "wedding_ring") == 0


@pytest.mark.asyncio
async def test_race_concurrent_acceptance_when_participant_marries_in_other_proposal(db_session):
    """Race E: User A proposes to B, while C proposes to A.
    B accepts A->B at the exact same moment A accepts C->A.
    At most ONE Marriage involving A may be created."""
    user_a = await _mk_user(db_session, 7530)
    user_b = await _mk_user(db_session, 7531)
    user_c = await _mk_user(db_session, 7532)
    group = await _mk_group(db_session, -1008604)
    await _grant_ring(db_session, user_a.id, 1)
    await _grant_ring(db_session, user_c.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 17, 30, 0, tzinfo=timezone.utc)
    prop_ab = await MarriageProposalService.create_proposal(
        db_session, user_a.id, user_b.id, group.id, message_id=5505, now=t0
    )
    prop_ca = await MarriageProposalService.create_proposal(
        db_session, user_c.id, user_a.id, group.id, message_id=5506, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def accept_prop(pid: int, actor_id: int):
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, pid, actor_id, now=t0 + timedelta(seconds=15)
                )
                await s.commit()
                return res.marriage_id
            except ProposalError as exc:
                await s.rollback()
                return exc

    outcomes = await asyncio.gather(
        accept_prop(prop_ab.id, user_b.id),
        accept_prop(prop_ca.id, user_a.id),
    )
    winners = [o for o in outcomes if isinstance(o, int)]
    assert len(winners) == 1

    db_session.expunge_all()
    active_marriages = (
        await db_session.execute(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).scalars().all()
    assert len(active_marriages) == 1
    assert active_marriages[0].includes_user(user_a.id)


@pytest.mark.asyncio
async def test_race_concurrent_acceptance_cannot_bypass_divorce_cooldown(db_session):
    """Race F: A previously divorced user cannot bypass an active cooldown
    through concurrent acceptance requests."""
    proposer_a = await _mk_user(db_session, 7540)
    proposer_b = await _mk_user(db_session, 7541)
    target = await _mk_user(db_session, 7542)
    ex_spouse = await _mk_user(db_session, 7543)
    group = await _mk_group(db_session, -1008605)
    await _grant_ring(db_session, proposer_a.id, 1)
    await _grant_ring(db_session, proposer_b.id, 1)

    t0 = datetime(2026, 10, 3, 18, 0, 0, tzinfo=timezone.utc)
    prop_a = await MarriageProposalService.create_proposal(
        db_session, proposer_a.id, target.id, group.id, message_id=5507, now=t0
    )
    prop_b = await MarriageProposalService.create_proposal(
        db_session, proposer_b.id, target.id, group.id, message_id=5508, now=t0
    )

    # Target enters an active 24h divorce cooldown
    db_session.add(
        Marriage(
            proposer_id=target.id,
            accepter_id=ex_spouse.id,
            origin_group_id=group.id,
            status=MarriageStatus.DIVORCED,
            divorced_at=t0,
            cooldown_until=t0 + timedelta(seconds=86400),
        )
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def try_accept(pid: int):
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, pid, target.id, now=t0 + timedelta(seconds=30)
                )
                await s.commit()
                return res.marriage_id
            except MarriageCooldownActiveError as exc:
                await s.rollback()
                return exc

    outcomes = await asyncio.gather(
        try_accept(prop_a.id),
        try_accept(prop_b.id),
    )
    assert all(isinstance(o, MarriageCooldownActiveError) for o in outcomes)

    db_session.expunge_all()
    assert await MarriageService.is_user_married(db_session, target.id) is False
    assert await ItemService.get_total(db_session, proposer_a.id, "wedding_ring") == 1
    assert await ItemService.get_total(db_session, proposer_b.id, "wedding_ring") == 1



# ---------------------------------------------------------------------------
# 7. Lock-order regression coverage (Phase 5.1)
#
# ``MarriageService.accept_proposal`` now acquires User -> Marriage ->
# MarriageProposal, matching ``create_proposal`` (which has to lock the
# participant users first because it has no proposal id yet). These tests run
# acceptance concurrently with the proposal-creation path against the SAME
# user rows so a re-introduced inversion shows up as a lock failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_acceptance_versus_concurrent_outgoing_proposal_creation(db_session):
    """Race G: A->B is accepted while A concurrently proposes to C.

    Both transactions lock User A and A's proposal rows. Either the proposal
    lands first (then acceptance still succeeds) or acceptance wins (then the
    new proposal is refused by a domain rule). At most one ACTIVE marriage for
    A may exist either way.
    """
    user_a = await _mk_user(db_session, 7600)
    user_b = await _mk_user(db_session, 7601)
    user_c = await _mk_user(db_session, 7602)
    group = await _mk_group(db_session, -1008606)
    await _grant_ring(db_session, user_a.id, 2)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 18, 0, 0, tzinfo=timezone.utc)
    prop_ab = await MarriageProposalService.create_proposal(
        db_session, user_a.id, user_b.id, group.id, message_id=5601, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def do_accept():
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, prop_ab.id, user_b.id, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return res.marriage_id
            except ProposalError as exc:
                await s.rollback()
                return exc

    async def do_create():
        async with maker() as s:
            try:
                prop = await MarriageProposalService.create_proposal(
                    s, user_a.id, user_c.id, group.id, message_id=5602,
                    now=t0 + timedelta(seconds=10),
                )
                await s.commit()
                return prop.id
            except ProposalError as exc:
                await s.rollback()
                return exc

    outcomes = await asyncio.gather(do_accept(), do_create())
    accept_outcome, create_outcome = outcomes

    # Acceptance must always succeed: the proposal was already pending.
    assert isinstance(accept_outcome, int)
    # The concurrent proposal is either created (before the marriage existed)
    # or refused by a domain rule (A is married / A still has a live outgoing
    # proposal). What matters for this regression test is that it is a DOMAIN
    # error and never a lock/deadlock failure from the lock-order cycle.
    assert isinstance(create_outcome, int) or isinstance(create_outcome, ProposalError)

    db_session.expunge_all()
    active = (
        await db_session.execute(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).scalars().all()
    assert len(active) == 1
    assert active[0].includes_user(user_a.id)
    assert active[0].includes_user(user_b.id)


@pytest.mark.asyncio
async def test_race_acceptance_versus_concurrent_incoming_proposal_creation(db_session):
    """Race H: A->B is accepted while C concurrently proposes to B."""
    user_a = await _mk_user(db_session, 7610)
    user_b = await _mk_user(db_session, 7611)
    user_c = await _mk_user(db_session, 7612)
    group = await _mk_group(db_session, -1008607)
    await _grant_ring(db_session, user_a.id, 1)
    await _grant_ring(db_session, user_c.id, 1)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 18, 30, 0, tzinfo=timezone.utc)
    prop_ab = await MarriageProposalService.create_proposal(
        db_session, user_a.id, user_b.id, group.id, message_id=5611, now=t0
    )
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def do_accept():
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, prop_ab.id, user_b.id, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return res.marriage_id
            except ProposalError as exc:
                await s.rollback()
                return exc

    async def do_create():
        async with maker() as s:
            try:
                prop = await MarriageProposalService.create_proposal(
                    s, user_c.id, user_b.id, group.id, message_id=5612,
                    now=t0 + timedelta(seconds=10),
                )
                await s.commit()
                return prop.id
            except ProposalError as exc:
                await s.rollback()
                return exc

    accept_outcome, create_outcome = await asyncio.gather(do_accept(), do_create())

    assert isinstance(accept_outcome, int)
    assert isinstance(create_outcome, int) or isinstance(create_outcome, ProposalError)

    db_session.expunge_all()
    active = (
        await db_session.execute(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).scalars().all()
    assert len(active) == 1
    assert active[0].includes_user(user_b.id)
