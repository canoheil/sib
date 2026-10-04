"""Focused tests for Phase 6 — Divorce Domain Service (MarriageService)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from sib.database import (
    FlowerGift,
    FlowerGiftStatus,
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
from sib.services.love import LoveService
from sib.services.marriage import (
    MarriageAcceptanceResult,
    MarriageCooldownActiveError,
    MarriageDivorceRaceLostError,
    MarriageDivorceResult,
    MarriageError,
    MarriageNotActiveError,
    MarriageNotFoundError,
    MarriageService,
    NotMarriageSpouseError,
)
from sib.services.marriage_proposal import (
    MarriageProposalService,
    ProposalError,
    ProposalInvalidReason,
)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
COOLDOWN_SECONDS = 86400


async def _mk_user(session, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=f"user{user_id}", language_code="fa")
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = -1009501) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title="گروه طلاق")
        session.add(group)
        await session.flush()
    return group


async def _mk_marriage(
    session, a: int, b: int, *, love_score: int = 0, group_id: int = -1009501
) -> Marriage:
    await _mk_user(session, a)
    await _mk_user(session, b)
    await _mk_group(session, group_id)
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=group_id,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    session.add(marriage)
    await session.flush()
    return marriage


async def _mk_gift(
    session,
    marriage: Marriage,
    sender: int,
    receiver: int,
    *,
    gift_date: str = "2026-10-04",
) -> FlowerGift:
    gift = FlowerGift(
        marriage_id=marriage.id,
        sender_id=sender,
        receiver_id=receiver,
        gift_date=gift_date,
        status=FlowerGiftStatus.PENDING,
        love_delta=5,
    )
    session.add(gift)
    await session.flush()
    return gift


async def _mk_proposal(
    session,
    proposer: int,
    target: int,
    *,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> MarriageProposal:
    await _mk_user(session, proposer)
    await _mk_user(session, target)
    await _mk_group(session)
    proposal = MarriageProposal(
        proposer_id=proposer,
        target_id=target,
        group_id=-1009501,
        status=ProposalStatus.PENDING,
        expires_at=expires_at,
    )
    session.add(proposal)
    await session.flush()
    return proposal


def _maker():
    return get_async_sessionmaker()


# ---------------------------------------------------------------------------
# 1. Successful divorce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_divorce_sets_status_timestamps_and_clears_love(db_session):
    marriage = await _mk_marriage(db_session, 9000, 9001, love_score=37)
    await db_session.commit()

    result = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9000, now=NOW
    )
    await db_session.commit()

    assert isinstance(result, MarriageDivorceResult)
    assert result.transition_won is True
    assert result.marriage_id == marriage.id
    assert result.actor_id == 9000
    assert result.spouse_id == 9001
    assert result.status == MarriageStatus.DIVORCED
    assert result.love_score == 0

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.divorced_by_user_id == 9000
    assert stored.love_score == 0
    # Timestamps are recorded in UTC.
    assert stored.divorced_at is not None
    assert stored.divorced_at.replace(tzinfo=timezone.utc) == NOW


@pytest.mark.asyncio
async def test_divorce_uses_divorce_alias(db_session):
    marriage = await _mk_marriage(db_session, 9010, 9011, love_score=5)
    await db_session.commit()

    result = await MarriageService.divorce(db_session, marriage.id, 9011, now=NOW)
    await db_session.commit()

    assert result.status == MarriageStatus.DIVORCED
    assert result.actor_id == 9011
    assert result.spouse_id == 9010


@pytest.mark.asyncio
async def test_love_resets_to_zero_on_divorce(db_session):
    marriage = await _mk_marriage(db_session, 9020, 9021, love_score=88)
    await db_session.commit()
    assert await LoveService.get_love_score(db_session, 9020) == 88

    await MarriageService.divorce_marriage(db_session, marriage.id, 9020, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    # The shared Love value of the (now DIVORCED) marriage is zeroed ...
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None and stored.love_score == 0
    # ... and neither spouse has an ACTIVE marriage to read Love from any more.
    assert await LoveService.get_love_score(db_session, 9020) is None
    assert await LoveService.get_love_score(db_session, 9021) is None


# ---------------------------------------------------------------------------
# 2. Authorization & state errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_spouse_cannot_divorce(db_session):
    marriage = await _mk_marriage(db_session, 9030, 9031, love_score=10)
    stranger = await _mk_user(db_session, 9032)
    await db_session.commit()

    with pytest.raises(NotMarriageSpouseError) as exc_info:
        await MarriageService.divorce_marriage(
            db_session, marriage.id, stranger.id, now=NOW
        )
    await db_session.commit()

    assert exc_info.value.marriage_id == marriage.id
    assert exc_info.value.actor_user_id == stranger.id

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None and stored.status == MarriageStatus.ACTIVE
    assert stored.love_score == 10


@pytest.mark.asyncio
async def test_divorce_unknown_marriage_raises_not_found(db_session):
    with pytest.raises(MarriageNotFoundError):
        await MarriageService.divorce_marriage(db_session, 987654, 9000, now=NOW)


@pytest.mark.asyncio
async def test_divorce_of_already_divorced_marriage_fails(db_session):
    marriage = await _mk_marriage(db_session, 9040, 9041, love_score=20)
    await db_session.commit()

    first = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9040, now=NOW
    )
    await db_session.commit()
    assert first.status == MarriageStatus.DIVORCED

    # A second, later attempt on the already-DIVORCED marriage.
    later = NOW + timedelta(hours=1)
    with pytest.raises(MarriageNotActiveError) as exc_info:
        await MarriageService.divorce_marriage(
            db_session, marriage.id, 9041, now=later
        )
    await db_session.commit()

    assert exc_info.value.marriage_id == marriage.id
    assert exc_info.value.current_status == MarriageStatus.DIVORCED

    # The original divorce is untouched (divorced_by / divorced_at unchanged).
    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None
    assert stored.divorced_by_user_id == 9040
    assert stored.divorced_at.replace(tzinfo=timezone.utc) == NOW


@pytest.mark.asyncio
async def test_repeated_divorce_call_is_idempotent_error(db_session):
    """Three identical calls: the first wins, the rest raise — never a double
    refund or a double cooldown write."""
    marriage = await _mk_marriage(db_session, 9050, 9051, love_score=15)
    await _mk_gift(db_session, marriage, 9050, 9051)
    await db_session.commit()

    first = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9050, now=NOW
    )
    await db_session.commit()
    assert first.invalidated_gift_count == 1
    assert await ItemService.get_total(db_session, 9050, "flower") == 1

    for _ in range(2):
        with pytest.raises(MarriageNotActiveError):
            await MarriageService.divorce_marriage(
                db_session, marriage.id, 9050, now=NOW + timedelta(seconds=5)
            )
        await db_session.commit()

    # Refunded exactly once, not three times.
    db_session.expunge_all()
    assert await ItemService.get_total(db_session, 9050, "flower") == 1
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None and stored.love_score == 0


# ---------------------------------------------------------------------------
# 3. Cooldown & post-divorce state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_applied_to_both_spouses(db_session):
    marriage = await _mk_marriage(db_session, 9060, 9061)
    await db_session.commit()

    await MarriageService.divorce_marriage(db_session, marriage.id, 9060, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None
    expected = NOW + timedelta(seconds=COOLDOWN_SECONDS)
    assert stored.cooldown_until is not None
    assert stored.cooldown_until.replace(tzinfo=timezone.utc) == expected

    for user_id in (9060, 9061):
        cooldown = await MarriageService.get_active_cooldown_for_user(
            db_session, user_id, now=NOW + timedelta(seconds=1)
        )
        assert cooldown is not None, f"spouse {user_id} has no cooldown"
        assert cooldown == expected

    # Expired exactly at the boundary.
    assert (
        await MarriageService.get_active_cooldown_for_user(
            db_session, 9060, now=expected
        )
        is None
    )
    # Still active one second before.
    assert (
        await MarriageService.get_active_cooldown_for_user(
            db_session, 9061, now=expected - timedelta(seconds=1)
        )
        is not None
    )


@pytest.mark.asyncio
async def test_cooldown_is_config_driven(db_session):
    marriage = await _mk_marriage(db_session, 9070, 9071)
    await ConfigService.set(db_session, "marriage_divorce_cooldown_seconds", 60)
    await db_session.commit()

    await MarriageService.divorce_marriage(db_session, marriage.id, 9070, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None
    assert stored.cooldown_until.replace(tzinfo=timezone.utc) == NOW + timedelta(
        seconds=60
    )


@pytest.mark.asyncio
async def test_after_divorce_neither_spouse_is_married_and_cannot_remarry(db_session):
    marriage = await _mk_marriage(db_session, 9080, 9081)
    await _mk_user(db_session, 9082)
    await ItemService.grant(db_session, 9080, "wedding_ring", 1)
    await ItemService.grant(db_session, 9082, "wedding_ring", 1)
    await db_session.commit()

    await MarriageService.divorce_marriage(db_session, marriage.id, 9080, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    for user_id in (9080, 9081):
        assert await MarriageService.is_user_married(db_session, user_id) is False

    # Both spouses are blocked from ACCEPTING a new marriage during cooldown.
    for spouse_id in (9080, 9081):
        proposal = await _mk_proposal(db_session, 9082, spouse_id)
        await db_session.commit()
        with pytest.raises(MarriageCooldownActiveError) as exc_info:
            await MarriageService.accept_proposal(
                db_session, proposal.id, spouse_id, now=NOW + timedelta(minutes=1)
            )
        await db_session.commit()
        assert exc_info.value.user_id == spouse_id
        assert exc_info.value.remaining_seconds > 0

    # Once the cooldown expires the same acceptance path is allowed again.
    fresh = await _mk_proposal(
        db_session,
        9082,
        9080,
        expires_at=NOW + timedelta(seconds=COOLDOWN_SECONDS * 2),
    )
    await db_session.commit()
    accepted = await MarriageService.accept_proposal(
        db_session, fresh.id, 9080, now=NOW + timedelta(seconds=COOLDOWN_SECONDS + 1)
    )
    await db_session.commit()
    assert accepted.status == MarriageStatus.ACTIVE


@pytest.mark.asyncio
async def test_marriage_history_remains_after_divorce(db_session):
    marriage = await _mk_marriage(db_session, 9090, 9091, love_score=64)
    await _mk_gift(db_session, marriage, 9090, 9091)
    await db_session.commit()
    marriage_id = int(marriage.id)

    await MarriageService.divorce_marriage(db_session, marriage_id, 9090, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    # The row survives as DIVORCED, with its full audit trail.
    count = await db_session.scalar(
        select(func.count(Marriage.id)).where(Marriage.id == marriage_id)
    )
    assert count == 1
    stored = await db_session.get(Marriage, marriage_id)
    assert stored is not None
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.user1_id == 9090 and stored.user2_id == 9091
    assert stored.married_at is not None
    assert stored.divorced_at is not None
    assert stored.divorced_by_user_id == 9090


# ---------------------------------------------------------------------------
# 4. FlowerGift invalidation & refunds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_flowers_are_invalidated_and_refunded(db_session):
    marriage = await _mk_marriage(db_session, 9100, 9101, love_score=55)
    gift_ab = await _mk_gift(db_session, marriage, 9100, 9101)
    gift_ba = await _mk_gift(db_session, marriage, 9101, 9100)
    await db_session.commit()
    # Both flowers were already consumed at send time -> inventory is empty.
    assert await ItemService.get_total(db_session, 9100, "flower") == 0
    assert await ItemService.get_total(db_session, 9101, "flower") == 0

    result = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9100, now=NOW
    )
    await db_session.commit()

    assert result.invalidated_gift_count == 2
    assert result.flower_refund_count == 2

    db_session.expunge_all()
    # Each sender gets their own Flower back.
    assert await ItemService.get_total(db_session, 9100, "flower") == 1
    assert await ItemService.get_total(db_session, 9101, "flower") == 1

    for gift_id in (gift_ab.id, gift_ba.id):
        stored = await db_session.get(FlowerGift, gift_id)
        assert stored is not None
        assert stored.status == FlowerGiftStatus.INVALIDATED
        assert stored.resolved_at is not None

    # No PENDING gifts remain for the marriage.
    assert (
        await LoveService.get_pending_gifts_for_marriage(db_session, marriage.id) == []
    )


@pytest.mark.asyncio
async def test_divorce_invalidates_gifts_without_changing_love(db_session):
    """Invalidation must not apply any Love delta; divorce zeroes Love anyway,
    so these two tests pin both halves independently."""
    marriage = await _mk_marriage(db_session, 9110, 9111, love_score=30)
    await _mk_gift(db_session, marriage, 9110, 9111)
    await db_session.commit()

    await MarriageService.divorce_marriage(db_session, marriage.id, 9110, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage.id)
    assert stored is not None and stored.love_score == 0


@pytest.mark.asyncio
async def test_terminal_gifts_are_not_refunded_again(db_session):
    marriage = await _mk_marriage(db_session, 9120, 9121)
    accepted = await _mk_gift(db_session, marriage, 9120, 9121)
    rejected = await _mk_gift(db_session, marriage, 9121, 9120, gift_date="2026-10-01")
    pending = await _mk_gift(db_session, marriage, 9120, 9121, gift_date="2026-10-02")
    await ItemService.grant(db_session, 9120, "flower", 2)
    await ConfigService.set(db_session, "flower_love_gain", 5)
    await db_session.commit()

    await LoveService.accept_flower(db_session, accepted.id, 9121, now=NOW)
    await LoveService.reject_flower(db_session, rejected.id, 9120, now=NOW)
    await db_session.commit()
    # reject refunded 1 Flower to 9121; 9120 granted 2 then consumed 2 -> 0.
    pending_9120 = await ItemService.get_total(db_session, 9120, "flower")
    pending_9121 = await ItemService.get_total(db_session, 9121, "flower")

    result = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9120, now=NOW
    )
    await db_session.commit()

    # Only the still-PENDING gift is invalidated + refunded.
    assert result.invalidated_gift_count == 1
    assert result.invalidated_gifts[0].id == pending.id
    db_session.expunge_all()
    assert await ItemService.get_total(db_session, 9120, "flower") == pending_9120 + 1
    assert await ItemService.get_total(db_session, 9121, "flower") == pending_9121


# ---------------------------------------------------------------------------
# 5. Proposal invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_proposals_involving_spouses_are_invalidated(db_session):
    marriage = await _mk_marriage(db_session, 9130, 9131)
    outgoing = await _mk_proposal(db_session, 9130, 9132)  # spouse -> other
    incoming = await _mk_proposal(db_session, 9133, 9131)  # other -> spouse
    unrelated = await _mk_proposal(db_session, 9133, 9132)  # neither spouse
    await db_session.commit()

    result = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9130, now=NOW
    )
    await db_session.commit()

    assert result.invalidated_proposal_count == 2
    invalidated_ids = {p.id for p in result.invalidated_proposals}
    assert invalidated_ids == {outgoing.id, incoming.id}

    db_session.expunge_all()
    for proposal in (outgoing, incoming):
        stored = await db_session.get(MarriageProposal, proposal.id)
        assert stored is not None
        assert stored.status == ProposalStatus.INVALIDATED
        assert stored.invalid_reason == ProposalInvalidReason.USER_DIVORCED
        assert stored.resolved_at is not None

    unrelated_stored = await db_session.get(MarriageProposal, unrelated.id)
    assert unrelated_stored is not None
    assert unrelated_stored.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_ring_remains_intact_after_proposal_invalidation(db_session):
    """Proposals never consume a Wedding Ring, so invalidating them during a
    divorce must not consume or refund anything."""
    marriage = await _mk_marriage(db_session, 9140, 9141)
    await _mk_proposal(db_session, 9140, 9142)
    await _mk_proposal(db_session, 9143, 9141)
    await ItemService.grant(db_session, 9140, "wedding_ring", 3)
    await ItemService.grant(db_session, 9143, "wedding_ring", 2)
    await db_session.commit()

    result = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9140, now=NOW
    )
    await db_session.commit()

    assert result.invalidated_proposal_count == 2
    db_session.expunge_all()
    assert await ItemService.get_total(db_session, 9140, "wedding_ring") == 3
    assert await ItemService.get_total(db_session, 9143, "wedding_ring") == 2


# ---------------------------------------------------------------------------
# 6. Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_concurrent_duplicate_divorce(db_session):
    """Two simultaneous divorce calls: exactly one wins, the other raises, and
    the Flower is refunded exactly once."""
    marriage = await _mk_marriage(db_session, 9200, 9201, love_score=40)
    await _mk_gift(db_session, marriage, 9200, 9201)
    await db_session.commit()
    marriage_id = int(marriage.id)

    maker = _maker()

    async def do_divorce(actor_id: int):
        async with maker() as s:
            try:
                res = await MarriageService.divorce_marriage(
                    s, marriage_id, actor_id, now=NOW
                )
                await s.commit()
                return res
            except MarriageError as exc:
                await s.rollback()
                return exc

    outcomes = await asyncio.gather(do_divorce(9200), do_divorce(9201))
    winners = [o for o in outcomes if isinstance(o, MarriageDivorceResult)]
    losers = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], MarriageNotActiveError)

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage_id)
    assert stored is not None
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.love_score == 0
    # Exactly one refund.
    assert await ItemService.get_total(db_session, 9200, "flower") == 1
    gift_count = await db_session.scalar(
        select(func.count(FlowerGift.id)).where(
            FlowerGift.marriage_id == marriage_id,
            FlowerGift.status == FlowerGiftStatus.INVALIDATED,
        )
    )
    assert gift_count == 1


@pytest.mark.asyncio
async def test_race_divorce_versus_flower_acceptance(db_session):
    """Divorce racing with the receiver accepting a PENDING gift.

    Either the acceptance lands first (gift ACCEPTED, then Love zeroed by the
    divorce) or the divorce lands first (gift INVALIDATED + refunded, and the
    acceptance raises). Both are consistent; nothing is double-applied.
    """
    marriage = await _mk_marriage(db_session, 9210, 9211, love_score=20)
    gift = await _mk_gift(db_session, marriage, 9210, 9211)
    await ConfigService.set(db_session, "flower_love_gain", 5)
    await db_session.commit()
    marriage_id, gift_id = int(marriage.id), int(gift.id)

    maker = _maker()

    async def do_divorce():
        async with maker() as s:
            try:
                res = await MarriageService.divorce_marriage(
                    s, marriage_id, 9210, now=NOW
                )
                await s.commit()
                return ("DIVORCED", res.invalidated_gift_count)
            except MarriageError:
                await s.rollback()
                return None

    async def do_accept():
        async with maker() as s:
            try:
                res = await LoveService.accept_flower(
                    s, gift_id, 9211, now=NOW + timedelta(seconds=1)
                )
                await s.commit()
                return ("ACCEPTED", res.new_love)
            except Exception:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_divorce(), do_accept())
    assert outcomes[0] is not None  # divorce always wins the marriage transition

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage_id)
    stored_gift = await db_session.get(FlowerGift, gift_id)
    assert stored is not None and stored_gift is not None
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.love_score == 0
    assert stored_gift.status in (
        FlowerGiftStatus.ACCEPTED,
        FlowerGiftStatus.INVALIDATED,
    )
    # A refund happens if and only if the divorce invalidated the gift.
    expected_flowers = 1 if stored_gift.status == FlowerGiftStatus.INVALIDATED else 0
    assert await ItemService.get_total(db_session, 9210, "flower") == expected_flowers


@pytest.mark.asyncio
async def test_race_divorce_versus_flower_rejection(db_session):
    """Divorce racing with a rejection: the Flower is refunded at most once."""
    marriage = await _mk_marriage(db_session, 9220, 9221, love_score=20)
    gift = await _mk_gift(db_session, marriage, 9220, 9221)
    await db_session.commit()
    marriage_id, gift_id = int(marriage.id), int(gift.id)

    maker = _maker()

    async def do_divorce():
        async with maker() as s:
            try:
                await MarriageService.divorce_marriage(s, marriage_id, 9220, now=NOW)
                await s.commit()
                return "DIVORCED"
            except MarriageError:
                await s.rollback()
                return None

    async def do_reject():
        async with maker() as s:
            try:
                await LoveService.reject_flower(
                    s, gift_id, 9221, now=NOW + timedelta(seconds=1)
                )
                await s.commit()
                return "REJECTED"
            except Exception:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_divorce(), do_reject())
    assert outcomes[0] == "DIVORCED"

    db_session.expunge_all()
    stored_gift = await db_session.get(FlowerGift, gift_id)
    assert stored_gift is not None
    assert stored_gift.status in (
        FlowerGiftStatus.REJECTED,
        FlowerGiftStatus.INVALIDATED,
    )
    # Both branches refund exactly one Flower - never two.
    assert await ItemService.get_total(db_session, 9220, "flower") == 1


@pytest.mark.asyncio
async def test_race_divorce_versus_flower_invalidation(db_session):
    """Divorce racing with an explicit invalidation: refunded exactly once."""
    marriage = await _mk_marriage(db_session, 9230, 9231)
    gift = await _mk_gift(db_session, marriage, 9230, 9231)
    await db_session.commit()
    marriage_id, gift_id = int(marriage.id), int(gift.id)

    maker = _maker()

    async def do_divorce():
        async with maker() as s:
            try:
                await MarriageService.divorce_marriage(s, marriage_id, 9230, now=NOW)
                await s.commit()
                return "DIVORCED"
            except MarriageError:
                await s.rollback()
                return None

    async def do_invalidate():
        async with maker() as s:
            try:
                await LoveService.invalidate_gift(s, gift_id, now=NOW)
                await s.commit()
                return "INVALIDATED"
            except Exception:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_divorce(), do_invalidate())
    assert outcomes[0] == "DIVORCED"

    db_session.expunge_all()
    stored_gift = await db_session.get(FlowerGift, gift_id)
    assert stored_gift is not None
    assert stored_gift.status == FlowerGiftStatus.INVALIDATED
    assert await ItemService.get_total(db_session, 9230, "flower") == 1


@pytest.mark.asyncio
async def test_race_divorce_versus_proposal_acceptance(db_session):
    """A pending proposal to a spouse cannot turn into a marriage after the
    divorce: the cooldown (or the spouse-already-married check) stops it."""
    marriage = await _mk_marriage(db_session, 9240, 9241)
    await _mk_user(db_session, 9242)
    await ItemService.grant(db_session, 9242, "wedding_ring", 1)
    proposal = await _mk_proposal(db_session, 9242, 9241)
    await db_session.commit()
    marriage_id, proposal_id = int(marriage.id), int(proposal.id)

    maker = _maker()

    async def do_divorce():
        async with maker() as s:
            try:
                res = await MarriageService.divorce_marriage(
                    s, marriage_id, 9240, now=NOW
                )
                await s.commit()
                return res
            except MarriageError as exc:
                await s.rollback()
                return exc

    async def do_accept():
        async with maker() as s:
            try:
                res = await MarriageService.accept_proposal(
                    s, proposal_id, 9241, now=NOW + timedelta(seconds=1)
                )
                await s.commit()
                return res
            except ProposalError as exc:
                await s.rollback()
                return exc

    divorce_outcome, accept_outcome = await asyncio.gather(do_divorce(), do_accept())

    assert isinstance(divorce_outcome, MarriageDivorceResult)

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage_id)
    assert stored is not None and stored.status == MarriageStatus.DIVORCED

    active = (
        await db_session.execute(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).scalars().all()

    if isinstance(accept_outcome, MarriageAcceptanceResult):
        # Acceptance won the race: 9241 married 9242 before the divorce landed.
        assert len(active) == 1
        assert active[0].includes_user(9242)
    else:
        # Acceptance was refused - either the divorce invalidated the proposal
        # or the remarriage cooldown blocked it. What matters is that it is a
        # DOMAIN refusal and never a lock/deadlock failure.
        assert isinstance(accept_outcome, ProposalError)
        assert len(active) == 0


@pytest.mark.asyncio
async def test_race_divorce_versus_proposal_creation(db_session):
    """A proposal created concurrently with a divorce stays consistent: it is
    either created (and then un-acceptable due to the cooldown) or refused.
    Either way exactly one DIVORCED marriage exists and no lock error escapes.
    """
    marriage = await _mk_marriage(db_session, 9250, 9251)
    await _mk_user(db_session, 9252)
    await ItemService.grant(db_session, 9250, "wedding_ring", 1)
    await db_session.commit()
    marriage_id = int(marriage.id)

    maker = _maker()

    async def do_divorce():
        async with maker() as s:
            try:
                res = await MarriageService.divorce_marriage(
                    s, marriage_id, 9250, now=NOW
                )
                await s.commit()
                return res
            except MarriageError as exc:
                await s.rollback()
                return exc

    async def do_create():
        async with maker() as s:
            try:
                prop = await MarriageProposalService.create_proposal(
                    s, 9250, 9252, -1009501, now=NOW
                )
                await s.commit()
                return prop
            except ProposalError as exc:
                await s.rollback()
                return exc

    divorce_outcome, create_outcome = await asyncio.gather(do_divorce(), do_create())

    assert isinstance(divorce_outcome, MarriageDivorceResult)
    # Whatever happens, it is a domain outcome - never a lock/deadlock failure.
    assert isinstance(create_outcome, MarriageProposal) or isinstance(
        create_outcome, ProposalError
    )

    db_session.expunge_all()
    stored = await db_session.get(Marriage, marriage_id)
    assert stored is not None and stored.status == MarriageStatus.DIVORCED
    active = (
        await db_session.execute(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).scalars().all()
    assert len(active) == 0


@pytest.mark.asyncio
async def test_flower_refund_happens_exactly_once_under_repeated_invalidation(
    db_session,
):
    """Repeated invalidation attempts on the same gift must not double-refund."""
    marriage = await _mk_marriage(db_session, 9260, 9261)
    gift = await _mk_gift(db_session, marriage, 9260, 9261)
    await db_session.commit()

    first = await LoveService.invalidate_gift(db_session, gift.id, now=NOW)
    await db_session.commit()
    assert first.flower_refunded is True
    assert await ItemService.get_total(db_session, 9260, "flower") == 1

    with pytest.raises(Exception):
        await LoveService.invalidate_gift(db_session, gift.id, now=NOW)
    await db_session.commit()

    # Divorce afterwards must not refund it a second time either.
    result = await MarriageService.divorce_marriage(
        db_session, marriage.id, 9260, now=NOW
    )
    await db_session.commit()
    assert result.invalidated_gift_count == 0

    db_session.expunge_all()
    assert await ItemService.get_total(db_session, 9260, "flower") == 1


@pytest.mark.asyncio
async def test_divorce_cas_guard_rejects_a_marriage_ended_by_a_racing_writer(
    db_session, monkeypatch
):
    """Deterministic proof that the CAS (not just the pre-lock read) decides
    who won the divorce.

    On SQLite the write-intent barriers serialize the two callers, so the
    natural race always trips the pre-lock status check and the CAS rowcount
    guard is never reached. This flips the row to DIVORCED *inside* the lock
    step (without synchronizing the ORM copy), which is exactly the state a
    MariaDB/MySQL caller sees when a concurrent transaction commits between
    its read and its CAS.
    """
    marriage = await _mk_marriage(db_session, 9300, 9301, love_score=25)
    await db_session.commit()
    mid = int(marriage.id)

    original = MarriageService._lock_marriage

    async def _lock_then_race(session, marriage_id):
        locked = await original(session, marriage_id)
        # A concurrent writer commits the divorce here; the ORM copy ``locked``
        # still says ACTIVE because this UPDATE does not synchronize it.
        await session.execute(
            update(Marriage)
            .where(Marriage.id == marriage_id)
            .values(status=MarriageStatus.DIVORCED)
            .execution_options(synchronize_session=False)
        )
        return locked

    monkeypatch.setattr(MarriageService, "_lock_marriage", staticmethod(_lock_then_race))

    with pytest.raises(MarriageDivorceRaceLostError) as exc_info:
        await MarriageService.divorce_marriage(db_session, mid, 9300, now=NOW)
    await db_session.commit()

    assert exc_info.value.marriage_id == mid
    assert exc_info.value.current_status == MarriageStatus.DIVORCED
    # It is also catchable as a plain "marriage is not ACTIVE" error.
    assert isinstance(exc_info.value, MarriageNotActiveError)

    db_session.expunge_all()
    stored = await db_session.get(Marriage, mid)
    assert stored is not None
    assert stored.status == MarriageStatus.DIVORCED
    # The losing caller must not have written its own divorce fields.
    assert stored.divorced_by_user_id is None
    assert stored.cooldown_until is None


# ---------------------------------------------------------------------------
# 7. Phase 6.1 — Divorce cooldown enforcement for proposal creation
# ---------------------------------------------------------------------------


async def _divorce_and_arm_ring(db_session, a: int, b: int, target: int) -> int:
    """Divorce a<->b at NOW and give ``a`` a Wedding Ring.

    Returns the marriage id.
    """
    marriage = await _mk_marriage(db_session, a, b, love_score=10)
    await _mk_user(db_session, target)
    await ItemService.grant(db_session, a, "wedding_ring", 1)
    await db_session.commit()

    await MarriageService.divorce_marriage(db_session, marriage.id, a, now=NOW)
    await db_session.commit()
    return int(marriage.id)


async def _count_proposals(db_session) -> int:
    return int(
        await db_session.scalar(select(func.count(MarriageProposal.id))) or 0
    )


@pytest.mark.asyncio
async def test_proposer_with_active_cooldown_cannot_create_proposal(db_session):
    """Requirement 1: a proposer inside the post-divorce cooldown is refused."""
    await _divorce_and_arm_ring(db_session, 9400, 9401, 9402)
    db_session.expunge_all()

    with pytest.raises(MarriageCooldownActiveError) as exc_info:
        await MarriageProposalService.create_proposal(
            db_session, 9400, 9402, -1009501, now=NOW + timedelta(seconds=1)
        )
    await db_session.commit()

    assert exc_info.value.user_id == 9400
    assert exc_info.value.role == "proposer"
    assert exc_info.value.cooldown_until == NOW + timedelta(seconds=COOLDOWN_SECONDS)
    assert exc_info.value.remaining_seconds == COOLDOWN_SECONDS - 1


@pytest.mark.asyncio
async def test_no_proposal_created_and_ring_intact_on_cooldown_rejection(db_session):
    """Requirements 4 & 5: nothing is written and the ring is untouched."""
    await _divorce_and_arm_ring(db_session, 9410, 9411, 9412)
    db_session.expunge_all()
    assert await _count_proposals(db_session) == 0
    assert await ItemService.get_total(db_session, 9410, "wedding_ring") == 1

    for attempt in range(3):
        with pytest.raises(MarriageCooldownActiveError):
            await MarriageProposalService.create_proposal(
                db_session,
                9410,
                9412,
                -1009501,
                now=NOW + timedelta(seconds=attempt),
            )
        await db_session.commit()

    db_session.expunge_all()
    # No proposal row was created by any of the rejected attempts ...
    assert await _count_proposals(db_session) == 0
    # ... and the Wedding Ring was never consumed (proposals never consume it
    # anyway, and the rejection happens before the ring check).
    assert await ItemService.get_total(db_session, 9410, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_proposer_with_expired_cooldown_can_create_proposal(db_session):
    """Requirement 2: after the cooldown expires, creation behaves as before."""
    marriage_id = await _divorce_and_arm_ring(db_session, 9420, 9421, 9422)
    db_session.expunge_all()

    after_cooldown = NOW + timedelta(seconds=COOLDOWN_SECONDS + 1)
    proposal = await MarriageProposalService.create_proposal(
        db_session, 9420, 9422, -1009501, message_id=7001, now=after_cooldown
    )
    await db_session.commit()

    assert proposal.id is not None
    assert proposal.proposer_id == 9420
    assert proposal.target_id == 9422
    assert proposal.status == ProposalStatus.PENDING
    assert proposal.expires_at.replace(tzinfo=timezone.utc) == after_cooldown + timedelta(
        seconds=300
    )

    db_session.expunge_all()
    assert await _count_proposals(db_session) == 1
    assert await ItemService.get_total(db_session, 9420, "wedding_ring") == 1

    # The divorce itself is untouched by the proposal.
    stored = await db_session.get(Marriage, marriage_id)
    assert stored is not None and stored.status == MarriageStatus.DIVORCED


@pytest.mark.asyncio
async def test_proposal_creation_cooldown_boundary_is_exclusive(db_session):
    """Requirement 3: the cooldown is active *up to* cooldown_until and clear
    from cooldown_until onwards (``cooldown_until > now``).

    Uses a 60s cooldown so both sides of the boundary can be probed against a
    single divorce.
    """
    marriage = await _mk_marriage(db_session, 9430, 9431)
    await _mk_user(db_session, 9432)
    await _mk_user(db_session, 9433)
    await ItemService.grant(db_session, 9430, "wedding_ring", 2)
    await ConfigService.set(db_session, "marriage_divorce_cooldown_seconds", 60)
    await db_session.commit()

    await MarriageService.divorce_marriage(db_session, marriage.id, 9430, now=NOW)
    await db_session.commit()
    boundary = NOW + timedelta(seconds=60)

    # One second BEFORE the boundary: still cooling down -> refused.
    db_session.expunge_all()
    with pytest.raises(MarriageCooldownActiveError) as exc_info:
        await MarriageProposalService.create_proposal(
            db_session, 9430, 9432, -1009501, now=boundary - timedelta(seconds=1)
        )
    await db_session.commit()
    assert exc_info.value.remaining_seconds == 1
    assert await _count_proposals(db_session) == 0

    # EXACTLY at the boundary: allowed (matches the acceptance-side semantics:
    # the cooldown has elapsed, it is not "still active").
    db_session.expunge_all()
    at_boundary = await MarriageProposalService.create_proposal(
        db_session, 9430, 9432, -1009501, now=boundary
    )
    await db_session.commit()
    assert at_boundary.id is not None
    assert at_boundary.status == ProposalStatus.PENDING
    assert await _count_proposals(db_session) == 1


@pytest.mark.asyncio
async def test_target_cooldown_does_not_block_proposal_creation(db_session):
    """Scope check: only the PROPOSER's cooldown blocks creation.

    A proposal aimed at a cooling-down user is still created; that user's
    cooldown is enforced later, when they would have to accept it.
    """
    marriage = await _mk_marriage(db_session, 9440, 9441)
    await _mk_user(db_session, 9442)
    await ItemService.grant(db_session, 9442, "wedding_ring", 1)
    await db_session.commit()

    # 9440 is the proposer; the TARGET (9441) is the one who gets divorced.
    await MarriageService.divorce_marriage(db_session, marriage.id, 9440, now=NOW)
    await db_session.commit()

    db_session.expunge_all()
    # 9442 (no cooldown) may propose to the cooling-down 9441.
    proposal = await MarriageProposalService.create_proposal(
        db_session, 9442, 9441, -1009501, now=NOW + timedelta(seconds=1)
    )
    await db_session.commit()
    assert proposal.status == ProposalStatus.PENDING

    # But 9441 cannot accept it while cooling down (existing acceptance rule).
    with pytest.raises(MarriageCooldownActiveError):
        await MarriageService.accept_proposal(
            db_session, proposal.id, 9441, now=NOW + timedelta(seconds=2)
        )
    await db_session.commit()


@pytest.mark.asyncio
async def test_cooldown_check_precedes_pending_and_ring_checks(db_session):
    """Precedence: a cooling-down proposer is refused with the COOLDOWN error
    even when another precondition would also trip (a live outgoing proposal
    and no ring), so the error a handler sees is always the meaningful one."""
    marriage = await _mk_marriage(db_session, 9450, 9451)
    await db_session.commit()

    # Divorce first: this puts 9450 into the cooldown (and would itself
    # invalidate any proposal that already existed at that moment).
    await MarriageService.divorce_marriage(db_session, marriage.id, 9450, now=NOW)
    await db_session.commit()

    # Then stack the OTHER two preconditions on top, written directly so they
    # are not themselves blocked by the cooldown:
    #   (a) a live PENDING outgoing proposal for 9450
    #   (b) no Wedding Ring in 9450's inventory (deliberately never granted)
    pending = await _mk_proposal(
        db_session, 9450, 9452, expires_at=NOW + timedelta(hours=2)
    )
    await db_session.commit()
    assert await ItemService.get_total(db_session, 9450, "wedding_ring") == 0

    db_session.expunge_all()
    with pytest.raises(MarriageCooldownActiveError):
        await MarriageProposalService.create_proposal(
            db_session, 9450, 9452, -1009501, now=NOW + timedelta(seconds=1)
        )
    await db_session.commit()

    # The cooldown error is what surfaced, not ProposerHasPendingProposalError
    # and not WeddingRingRequiredError.
    db_session.expunge_all()
    stored = await db_session.get(MarriageProposal, pending.id)
    assert stored is not None and stored.status == ProposalStatus.PENDING
    assert await _count_proposals(db_session) == 1  # only the fixture one
