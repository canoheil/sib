"""Focused tests for Phase 5 — Sib Love + Flower Gift Service."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from sib.database import (
    FlowerGift,
    FlowerGiftStatus,
    Group,
    Marriage,
    MarriageStatus,
    User,
)
from sib.database.connection import get_async_sessionmaker
from sib.tests.conftest import requires_row_locks
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.love import (
    FlowerDailyLimitError,
    FlowerGiftAlreadyResolvedError,
    FlowerGiftMarriageInactiveError,
    FlowerGiftResult,
    FlowerLoveGainNotConfiguredError,
    FlowerRequiredError,
    LoveService,
    LoveServiceError,
    NotFlowerGiftReceiverError,
    NotMarriedError,
    NotSpouseError,
    SelfFlowerGiftError,
)


async def _mk_user(session, user_id: int, name: str = "کاربر") -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=f"{name}{user_id}", language_code="fa")
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = -1009001, title: str = "گروه عشق سیب") -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title=title)
        session.add(group)
        await session.flush()
    return group


async def _mk_marriage(
    session,
    user_a_id: int,
    user_b_id: int,
    *,
    group_id: int = -1009001,
    love_score: int = 0,
    status: str = MarriageStatus.ACTIVE,
) -> Marriage:
    await _mk_user(session, user_a_id)
    await _mk_user(session, user_b_id)
    await _mk_group(session, group_id)
    marriage = Marriage(
        proposer_id=user_a_id,
        accepter_id=user_b_id,
        origin_group_id=group_id,
        status=status,
        love_score=love_score,
    )
    session.add(marriage)
    await session.flush()
    return marriage


async def _grant_flower(session, user_id: int, quantity: int = 1) -> None:
    await ItemService.grant(session, user_id, "flower", quantity)
    await session.flush()


async def _configure_love_gain(session, gain: int = 5) -> None:
    await ConfigService.set(session, "flower_love_gain", gain)
    await session.flush()


# ---------------------------------------------------------------------------
# 1. Send Flower Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_flower_success_consumes_flower_and_creates_pending_gift(db_session):
    marriage = await _mk_marriage(db_session, 8001, 8002, love_score=10)
    await _grant_flower(db_session, 8001, 2)
    await _configure_love_gain(db_session, 7)
    await db_session.commit()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    res = await LoveService.send_flower(
        db_session,
        sender_user_id=8001,
        receiver_user_id=8002,
        chat_id=-1009001,
        message_id=3001,
        now=now,
    )
    await db_session.commit()

    assert isinstance(res, FlowerGiftResult)
    assert res.gift.id is not None
    assert res.marriage_id == marriage.id
    assert res.sender_id == 8001
    assert res.receiver_id == 8002
    assert res.chat_id == -1009001
    assert res.message_id == 3001
    assert res.gift_date == "2026-10-03"
    assert res.status == FlowerGiftStatus.PENDING
    assert res.love_delta == 7
    assert res.previous_love == 10
    assert res.new_love == 10
    assert res.flower_consumed is True
    assert res.flower_refunded is False

    # Exactly 1 Flower consumed at send time (2 -> 1)
    assert await ItemService.get_total(db_session, 8001, "flower") == 1


@pytest.mark.asyncio
async def test_send_flower_fails_when_flower_love_gain_unset_without_consuming(db_session):
    await _mk_marriage(db_session, 8010, 8011)
    await _grant_flower(db_session, 8010, 1)
    # The economy review gave flower_love_gain a real default (2), so this
    # test unsets it explicitly to exercise the "not configured" guard.
    await ConfigService.set(db_session, "flower_love_gain", None)
    await db_session.commit()

    with pytest.raises(FlowerLoveGainNotConfiguredError):
        await LoveService.send_flower(
            db_session,
            sender_user_id=8010,
            receiver_user_id=8011,
            chat_id=-1009001,
            message_id=3002,
        )

    # Flower was NOT consumed and no FlowerGift row was created
    assert await ItemService.get_total(db_session, 8010, "flower") == 1
    count = await db_session.scalar(select(func.count(FlowerGift.id)))
    assert count == 0


@pytest.mark.asyncio
async def test_send_flower_rejects_self_unmarried_non_spouse_and_missing_flower(db_session):
    await _mk_marriage(db_session, 8020, 8021)
    single_user = await _mk_user(db_session, 8022)
    await _mk_marriage(db_session, 8023, 8024)
    await _configure_love_gain(db_session, 5)
    await _grant_flower(db_session, 8020, 1)
    await _grant_flower(db_session, single_user.id, 1)
    await db_session.commit()

    # 1. Self-send rejected
    with pytest.raises(SelfFlowerGiftError):
        await LoveService.send_flower(db_session, 8020, 8020)

    # 2. Non-married user cannot send
    with pytest.raises(NotMarriedError):
        await LoveService.send_flower(db_session, single_user.id, 8020)

    # 3. Married user cannot send to a non-spouse
    with pytest.raises(NotSpouseError) as exc_info:
        await LoveService.send_flower(db_session, 8020, 8023)
    assert exc_info.value.actual_spouse_id == 8021

    # 4. Spouse without Flower in inventory is rejected
    with pytest.raises(FlowerRequiredError):
        await LoveService.send_flower(db_session, 8021, 8020)

    # Sender 8020 still has their 1 Flower untouched
    assert await ItemService.get_total(db_session, 8020, "flower") == 1


@pytest.mark.asyncio
async def test_send_flower_atomicity_rolls_back_consumption_if_gift_creation_fails(
    db_session, monkeypatch
):
    await _mk_marriage(db_session, 8030, 8031)
    await _grant_flower(db_session, 8030, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    async def _fail_create(*args, **kwargs):
        raise RuntimeError("Simulated DB error creating FlowerGift")

    monkeypatch.setattr(LoveService, "_create_flower_gift_record", _fail_create)

    with pytest.raises(RuntimeError, match="Simulated DB error"):
        await LoveService.send_flower(db_session, 8030, 8031)
    await db_session.commit()

    # Flower consumption rolled back completely
    assert await ItemService.get_total(db_session, 8030, "flower") == 1
    count = await db_session.scalar(select(func.count(FlowerGift.id)))
    assert count == 0


# ---------------------------------------------------------------------------
# 2. Daily Directional Limit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_directional_limit_pending_and_accepted_block_same_day(db_session):
    await _mk_marriage(db_session, 8101, 8102)
    await _grant_flower(db_session, 8101, 3)
    await _grant_flower(db_session, 8102, 2)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    day1 = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)

    # 1. A -> B first gift allowed
    first_ab = await LoveService.send_flower(
        db_session, 8101, 8102, now=day1
    )
    await db_session.commit()

    # 2. A -> B second gift while first is PENDING is blocked
    with pytest.raises(FlowerDailyLimitError):
        await LoveService.send_flower(
            db_session, 8101, 8102, now=day1 + timedelta(minutes=5)
        )
    # No extra flower consumed on blocked send
    assert await ItemService.get_total(db_session, 8101, "flower") == 2

    # 3. B -> A is independent and allowed on the same UTC day!
    first_ba = await LoveService.send_flower(
        db_session, 8102, 8101, now=day1 + timedelta(minutes=10)
    )
    await db_session.commit()
    assert first_ba.status == FlowerGiftStatus.PENDING

    # 4. B accepts A -> B; A -> B is now ACCEPTED and still blocks a second A -> B today
    await LoveService.accept_flower(
        db_session, first_ab.id, 8102, now=day1 + timedelta(minutes=15)
    )
    await db_session.commit()

    with pytest.raises(FlowerDailyLimitError):
        await LoveService.send_flower(
            db_session, 8101, 8102, now=day1 + timedelta(hours=2)
        )

    # 5. Next UTC day resets directional allowance for A -> B
    day2 = datetime(2026, 10, 4, 0, 5, 0, tzinfo=timezone.utc)
    next_day_ab = await LoveService.send_flower(
        db_session, 8101, 8102, now=day2
    )
    await db_session.commit()
    assert next_day_ab.gift_date == "2026-10-04"
    assert next_day_ab.status == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_rejected_and_invalidated_gifts_refund_flower_and_free_daily_slot(db_session):
    await _mk_marriage(db_session, 8110, 8111, love_score=10)
    # Give 8110 only 1 Flower: proves that refund + daily slot release both work
    await _grant_flower(db_session, 8110, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    now = datetime(2026, 10, 3, 14, 0, 0, tzinfo=timezone.utc)

    # Attempt 1: sent (1 -> 0 flowers), then rejected (0 -> 1 flower refunded, Love 10 -> 9)
    g1 = await LoveService.send_flower(db_session, 8110, 8111, now=now)
    assert await ItemService.get_total(db_session, 8110, "flower") == 0

    rej = await LoveService.reject_flower(
        db_session, g1.id, 8111, now=now + timedelta(minutes=1)
    )
    await db_session.commit()
    assert rej.flower_refunded is True
    assert rej.new_love == 9
    assert await ItemService.get_total(db_session, 8110, "flower") == 1

    # Attempt 2 (same day): allowed using the refunded Flower! Then invalidated (0 -> 1 flower)
    g2 = await LoveService.send_flower(
        db_session, 8110, 8111, now=now + timedelta(minutes=2)
    )
    inv = await LoveService.invalidate_gift(
        db_session, g2.id, now=now + timedelta(minutes=3)
    )
    await db_session.commit()
    assert inv.flower_refunded is True
    assert inv.new_love == 9  # unchanged on invalidation
    assert await ItemService.get_total(db_session, 8110, "flower") == 1

    # Attempt 3 (same day): allowed again and accepted!
    g3 = await LoveService.send_flower(
        db_session, 8110, 8111, now=now + timedelta(minutes=4)
    )
    acc = await LoveService.accept_flower(
        db_session, g3.id, 8111, now=now + timedelta(minutes=5)
    )
    await db_session.commit()
    assert acc.status == FlowerGiftStatus.ACCEPTED
    assert acc.new_love == 14
    assert await ItemService.get_total(db_session, 8110, "flower") == 0


# ---------------------------------------------------------------------------
# 3. Flower Acceptance & Love Clamping Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flower_authorization_love_clamping_and_max_100_behavior(db_session):
    marriage = await _mk_marriage(db_session, 8201, 8202, love_score=97)
    stranger = await _mk_user(db_session, 8203)
    await _grant_flower(db_session, 8201, 1)
    await _grant_flower(db_session, 8202, 1)
    await _configure_love_gain(db_session, 10)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 15, 0, 0, tzinfo=timezone.utc)
    gift_ab = await LoveService.send_flower(db_session, 8201, 8202, now=t0)
    await db_session.commit()

    # Sender and stranger cannot accept
    for bad_actor in (8201, stranger.id):
        with pytest.raises(NotFlowerGiftReceiverError):
            await LoveService.accept_flower(db_session, gift_ab.id, bad_actor, now=t0)

    # Receiver accepts: 97 + 10 clamps to 100
    accept_time = t0 + timedelta(minutes=2)
    res1 = await LoveService.accept_flower(
        db_session, gift_ab.id, 8202, now=accept_time
    )
    await db_session.commit()

    assert res1.status == FlowerGiftStatus.ACCEPTED
    assert res1.previous_love == 97
    assert res1.new_love == 100
    assert res1.love_delta == 3  # clamped delta from 97 -> 100
    assert res1.marriage.last_flower_accepted_date == "2026-10-03"
    assert res1.marriage.last_flower_accepted_at is not None
    # Flower not consumed again
    assert await ItemService.get_total(db_session, 8201, "flower") == 0

    # Now Love is ALREADY 100; B -> A sends a flower and A accepts -> still succeeds at 100!
    gift_ba = await LoveService.send_flower(
        db_session, 8202, 8201, now=accept_time + timedelta(minutes=1)
    )
    res2 = await LoveService.accept_flower(
        db_session, gift_ba.id, 8201, now=accept_time + timedelta(minutes=2)
    )
    await db_session.commit()

    assert res2.status == FlowerGiftStatus.ACCEPTED
    assert res2.previous_love == 100
    assert res2.new_love == 100
    assert res2.love_delta == 0
    await db_session.refresh(marriage)
    assert marriage.love_score == 100


# ---------------------------------------------------------------------------
# 4. Flower Rejection & Clamping at 0 Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_flower_authorization_penalty_clamp_at_zero_and_refund(db_session):
    marriage = await _mk_marriage(db_session, 8301, 8302, love_score=0)
    await _grant_flower(db_session, 8301, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 16, 0, 0, tzinfo=timezone.utc)
    gift = await LoveService.send_flower(db_session, 8301, 8302, now=t0)
    await db_session.commit()
    assert await ItemService.get_total(db_session, 8301, "flower") == 0

    # Sender cannot reject their own gift
    with pytest.raises(NotFlowerGiftReceiverError):
        await LoveService.reject_flower(db_session, gift.id, 8301, now=t0)

    # Receiver rejects at Love=0 -> clamps at 0, refunds 1 Flower to sender
    rej = await LoveService.reject_flower(
        db_session, gift.id, 8302, now=t0 + timedelta(seconds=30)
    )
    await db_session.commit()

    assert rej.status == FlowerGiftStatus.REJECTED
    assert rej.previous_love == 0
    assert rej.new_love == 0
    assert rej.flower_refunded is True
    assert await ItemService.get_total(db_session, 8301, "flower") == 1
    await db_session.refresh(marriage)
    assert marriage.love_score == 0

    # Cannot reject a second time
    with pytest.raises(FlowerGiftAlreadyResolvedError):
        await LoveService.reject_flower(db_session, gift.id, 8302, now=t0 + timedelta(seconds=60))
    assert await ItemService.get_total(db_session, 8301, "flower") == 1


# ---------------------------------------------------------------------------
# 5. Invalidation & Expiration Preparation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_and_expire_gift_refund_without_changing_love(db_session):
    marriage = await _mk_marriage(db_session, 8401, 8402, love_score=42)
    await _grant_flower(db_session, 8401, 1)
    await _grant_flower(db_session, 8402, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 17, 0, 0, tzinfo=timezone.utc)
    gift_ab = await LoveService.send_flower(db_session, 8401, 8402, now=t0)
    gift_ba = await LoveService.send_flower(db_session, 8402, 8401, now=t0)
    await db_session.commit()

    inv = await LoveService.invalidate_gift(
        db_session, gift_ab.id, now=t0 + timedelta(minutes=1)
    )
    exp = await LoveService.expire_gift(
        db_session, gift_ba.id, now=t0 + timedelta(minutes=2)
    )
    await db_session.commit()

    assert inv.status == FlowerGiftStatus.INVALIDATED
    assert inv.flower_refunded is True
    assert inv.previous_love == 42 and inv.new_love == 42

    assert exp.status == FlowerGiftStatus.EXPIRED
    assert exp.flower_refunded is True
    assert exp.previous_love == 42 and exp.new_love == 42

    assert await ItemService.get_total(db_session, 8401, "flower") == 1
    assert await ItemService.get_total(db_session, 8402, "flower") == 1
    await db_session.refresh(marriage)
    assert marriage.love_score == 42

    # Terminal gifts cannot be invalidated again
    with pytest.raises(FlowerGiftAlreadyResolvedError):
        await LoveService.invalidate_gift(db_session, gift_ab.id)
    with pytest.raises(FlowerGiftAlreadyResolvedError):
        await LoveService.invalidate_gift(db_session, gift_ba.id)


# ---------------------------------------------------------------------------
# 6. Marriage Interaction (Inactive Marriage at Resolution Time)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gift_cannot_resolve_after_marriage_becomes_inactive_and_refunds_flower(
    db_session,
):
    marriage = await _mk_marriage(db_session, 8501, 8502, love_score=25)
    await _grant_flower(db_session, 8501, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 18, 0, 0, tzinfo=timezone.utc)
    gift = await LoveService.send_flower(db_session, 8501, 8502, now=t0)
    await db_session.commit()
    assert await ItemService.get_total(db_session, 8501, "flower") == 0

    # Marriage becomes DIVORCED before gift is resolved
    marriage.status = MarriageStatus.DIVORCED
    marriage.love_score = 0
    await db_session.commit()

    with pytest.raises(FlowerGiftMarriageInactiveError) as exc_info:
        await LoveService.accept_flower(
            db_session, gift.id, 8502, now=t0 + timedelta(minutes=1)
        )
    await db_session.commit()

    assert exc_info.value.flower_refunded is True
    # Love was NOT modified and Flower was refunded to sender
    await db_session.refresh(marriage)
    assert marriage.love_score == 0
    assert await ItemService.get_total(db_session, 8501, "flower") == 1
    refreshed_gift = await LoveService.get_gift(db_session, gift.id)
    assert refreshed_gift is not None
    assert refreshed_gift.status == FlowerGiftStatus.INVALIDATED


# ---------------------------------------------------------------------------
# 7. Concurrency & Race Safety Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_simultaneous_send_flower_creates_at_most_one_pending_gift(db_session):
    """Race A: Two simultaneous send_flower calls by the same sender.
    Only one Flower may be consumed and only one PENDING gift created."""
    marriage = await _mk_marriage(db_session, 8601, 8602)
    await _grant_flower(db_session, 8601, 2)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    maker = get_async_sessionmaker()
    t0 = datetime(2026, 10, 3, 19, 0, 0, tzinfo=timezone.utc)

    async def try_send(msg_id: int):
        async with maker() as s:
            try:
                res = await LoveService.send_flower(
                    s, 8601, 8602, chat_id=-1009001, message_id=msg_id, now=t0
                )
                await s.commit()
                return res.gift_id
            except LoveServiceError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(try_send(4001), try_send(4002))
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1

    db_session.expunge_all()
    assert await ItemService.get_total(db_session, 8601, "flower") == 1
    pending = await LoveService.get_pending_gifts_for_marriage(db_session, marriage.id)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_race_simultaneous_accept_flower_increments_love_once(db_session):
    """Race B: Two simultaneous accept_flower calls.
    Exactly one terminal transition wins; Love increases once."""
    marriage = await _mk_marriage(db_session, 8610, 8611, love_score=20)
    await _grant_flower(db_session, 8610, 1)
    await _configure_love_gain(db_session, 6)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 19, 15, 0, tzinfo=timezone.utc)
    gift = await LoveService.send_flower(db_session, 8610, 8611, now=t0)
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def try_accept():
        async with maker() as s:
            try:
                res = await LoveService.accept_flower(
                    s, gift.id, 8611, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return res.new_love
            except LoveServiceError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(try_accept(), try_accept())
    winners = [o for o in outcomes if o is not None]
    assert winners == [26]

    db_session.expunge_all()
    refreshed_m = await db_session.get(Marriage, marriage.id)
    assert refreshed_m is not None
    assert refreshed_m.love_score == 26


@pytest.mark.asyncio
async def test_race_accept_versus_reject_produces_single_consistent_outcome(db_session):
    """Race C: Accept racing with reject.
    Exactly one terminal state wins; Love and inventory reflect only the winner."""
    marriage = await _mk_marriage(db_session, 8620, 8621, love_score=30)
    await _grant_flower(db_session, 8620, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 19, 30, 0, tzinfo=timezone.utc)
    gift = await LoveService.send_flower(db_session, 8620, 8621, now=t0)
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def do_accept():
        async with maker() as s:
            try:
                res = await LoveService.accept_flower(
                    s, gift.id, 8621, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return ("ACCEPTED", res.new_love)
            except LoveServiceError:
                await s.rollback()
                return None

    async def do_reject():
        async with maker() as s:
            try:
                res = await LoveService.reject_flower(
                    s, gift.id, 8621, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return ("REJECTED", res.new_love)
            except LoveServiceError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_accept(), do_reject())
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    winning_status, expected_love = winners[0]

    db_session.expunge_all()
    final_gift = await LoveService.get_gift(db_session, gift.id)
    final_marriage = await db_session.get(Marriage, marriage.id)
    assert final_gift is not None and final_marriage is not None
    assert final_gift.status == winning_status
    assert final_marriage.love_score == expected_love

    if winning_status == FlowerGiftStatus.ACCEPTED:
        assert expected_love == 35
        assert await ItemService.get_total(db_session, 8620, "flower") == 0
    else:
        assert expected_love == 29
        assert await ItemService.get_total(db_session, 8620, "flower") == 1


@pytest.mark.asyncio
async def test_race_reject_versus_invalidate_refunds_exactly_once(db_session):
    """Race D: Reject racing with invalidate.
    Exactly one terminal state wins and the sender receives at most 1 refunded Flower."""
    marriage = await _mk_marriage(db_session, 8630, 8631, love_score=15)
    await _grant_flower(db_session, 8630, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 19, 45, 0, tzinfo=timezone.utc)
    gift = await LoveService.send_flower(db_session, 8630, 8631, now=t0)
    await db_session.commit()
    assert await ItemService.get_total(db_session, 8630, "flower") == 0

    maker = get_async_sessionmaker()

    async def do_reject():
        async with maker() as s:
            try:
                res = await LoveService.reject_flower(
                    s, gift.id, 8631, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return res.status
            except LoveServiceError:
                await s.rollback()
                return None

    async def do_invalidate():
        async with maker() as s:
            try:
                res = await LoveService.invalidate_gift(
                    s, gift.id, now=t0 + timedelta(seconds=10)
                )
                await s.commit()
                return res.status
            except LoveServiceError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_reject(), do_invalidate())
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    assert winners[0] in (FlowerGiftStatus.REJECTED, FlowerGiftStatus.INVALIDATED)

    db_session.expunge_all()
    # Refunded exactly ONCE (0 -> 1, never 2)
    assert await ItemService.get_total(db_session, 8630, "flower") == 1
    final_marriage = await db_session.get(Marriage, marriage.id)
    assert final_marriage is not None
    if winners[0] == FlowerGiftStatus.REJECTED:
        assert final_marriage.love_score == 14
    else:
        assert final_marriage.love_score == 15


async def _run_send_vs_external_consume(db_session, marriage_id: int):
    """Run ``send_flower`` concurrently with an external Flower consumption."""
    maker = get_async_sessionmaker()
    t0 = datetime(2026, 10, 3, 20, 0, 0, tzinfo=timezone.utc)

    async def do_send():
        async with maker() as s:
            try:
                res = await LoveService.send_flower(s, 8640, 8641, now=t0)
                await s.commit()
                return ("SENT", res.gift_id)
            except LoveServiceError:
                await s.rollback()
                return None

    async def do_consume_elsewhere():
        async with maker() as s:
            try:
                await ItemService.consume(s, 8640, "flower", 1)
                await s.commit()
                return ("CONSUMED", None)
            except Exception:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_send(), do_consume_elsewhere())
    db_session.expunge_all()
    inventory = await ItemService.get_total(db_session, 8640, "flower")
    pending = await LoveService.get_pending_gifts_for_marriage(db_session, marriage_id)
    return outcomes, inventory, pending


async def _seed_send_vs_external_consume(db_session) -> int:
    marriage = await _mk_marriage(db_session, 8640, 8641)
    await _grant_flower(db_session, 8640, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()
    return int(marriage.id)


@pytest.mark.asyncio
async def test_race_send_flower_versus_concurrent_flower_consumption(db_session):
    """Race E: send_flower racing with another operation consuming the sender's
    only Flower.

    These are the invariants that hold on EVERY engine: the send either
    succeeds or fails with a domain error (never corrupts state), inventory
    never goes negative, and a PENDING gift exists if and only if the send
    succeeded.
    """
    marriage_id = await _seed_send_vs_external_consume(db_session)

    outcomes, inventory, pending = await _run_send_vs_external_consume(
        db_session, marriage_id
    )
    sent = any(o is not None and o[0] == "SENT" for o in outcomes)

    assert 0 <= inventory <= 1, f"inventory went out of range: {inventory}"
    assert len(pending) == (1 if sent else 0)


@pytest.mark.asyncio
@requires_row_locks
async def test_race_send_flower_versus_concurrent_flower_consumption_is_exclusive(
    db_session,
):
    """Race E (strict): exactly one of the two operations may consume the unit.

    Gated on a row-lock engine: SQLite silently ignores ``SELECT ... FOR
    UPDATE``, so ``ItemService.consume``'s read-modify-write is not atomic
    there and two concurrent consumes can both observe the last unit.
    """
    marriage_id = await _seed_send_vs_external_consume(db_session)

    outcomes, inventory, pending = await _run_send_vs_external_consume(
        db_session, marriage_id
    )
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    assert inventory == 0
    assert len(pending) == (1 if winners[0][0] == "SENT" else 0)


# ---------------------------------------------------------------------------
# 7b. Cross-path lock-order regression coverage (Phase 5.1)
#
# ``send_flower`` (User -> Marriage -> FlowerGift -> UserItem) and the gift
# resolution paths now take their locks in the same global order. These tests
# run the two paths concurrently against the SAME User, Marriage and UserItem
# rows so a re-introduced lock-order inversion would produce a lock-cycle
# failure instead of silently passing.
# ---------------------------------------------------------------------------


async def _concurrent_flowers(
    db_session,
    *,
    user_a: int,
    user_b: int,
    old_gift_id: int,
    old_gift_sender: int,
    old_gift_receiver: int,
    resolution: str,
    flowers: int = 1,
):
    """Run ``send_flower(user_a -> user_b)`` concurrently with a resolution of
    ``old_gift_id`` and return ``(outcomes, inventory, love, old_status)``.
    """
    maker = get_async_sessionmaker()
    now = datetime(2026, 10, 3, 21, 0, 0, tzinfo=timezone.utc)

    async def do_send():
        async with maker() as s:
            try:
                res = await LoveService.send_flower(
                    s, user_a, user_b, chat_id=-1009001, message_id=5001, now=now
                )
                await s.commit()
                return ("SENT", res.gift_id)
            except LoveServiceError:
                await s.rollback()
                return None

    async def do_resolve():
        async with maker() as s:
            try:
                if resolution == "accept":
                    res = await LoveService.accept_flower(
                        s, old_gift_id, old_gift_receiver, now=now
                    )
                elif resolution == "reject":
                    res = await LoveService.reject_flower(
                        s, old_gift_id, old_gift_receiver, now=now
                    )
                else:
                    res = await LoveService.invalidate_gift(
                        s, old_gift_id, now=now
                    )
                await s.commit()
                return (resolution.upper(), res.new_love)
            except LoveServiceError:
                await s.rollback()
                return None

    outcomes = await asyncio.gather(do_send(), do_resolve())

    db_session.expunge_all()
    inventory = await ItemService.get_total(db_session, user_a, "flower")
    refreshed = await LoveService.get_gift(db_session, old_gift_id)
    love = await LoveService.get_love_score(db_session, user_a)
    assert refreshed is not None
    return outcomes, inventory, love, refreshed.status


@pytest.mark.asyncio
async def test_race_send_flower_versus_flower_reject(db_session):
    """Flower send racing with a rejection (refund) on the same couple.

    Both transactions touch User A, User B, the shared Marriage and A's
    UserItem row. Exactly one Flower is consumed by the send and exactly one
    is refunded by the rejection, and Love takes the rejection penalty once.
    """
    marriage = await _mk_marriage(db_session, 8700, 8701, love_score=20)
    # Older gift dated in the past so it does not consume today's A -> B quota.
    old_gift = FlowerGift(
        marriage_id=marriage.id,
        sender_id=8700,
        receiver_id=8701,
        gift_date="2026-10-01",
        status=FlowerGiftStatus.PENDING,
        love_delta=5,
    )
    db_session.add(old_gift)
    await _grant_flower(db_session, 8700, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    outcomes, inventory, love, old_status = await _concurrent_flowers(
        db_session,
        user_a=8700,
        user_b=8701,
        old_gift_id=old_gift.id,
        old_gift_sender=8700,
        old_gift_receiver=8701,
        resolution="reject",
    )
    assert outcomes == [("SENT", outcomes[0][1]), ("REJECT", 19)]
    # 1 granted - 1 consumed by the send + 1 refunded by the rejection
    assert inventory == 1
    assert love == 19
    assert old_status == FlowerGiftStatus.REJECTED
    pending = await LoveService.get_pending_gifts_for_marriage(db_session, marriage.id)
    assert len(pending) == 1
    assert pending[0].sender_id == 8700


@pytest.mark.asyncio
async def test_race_send_flower_versus_flower_accept(db_session):
    """Flower send racing with an acceptance on the same couple."""
    marriage = await _mk_marriage(db_session, 8710, 8711, love_score=20)
    old_gift = FlowerGift(
        marriage_id=marriage.id,
        sender_id=8711,
        receiver_id=8710,
        gift_date="2026-10-01",
        status=FlowerGiftStatus.PENDING,
        love_delta=5,
    )
    db_session.add(old_gift)
    await _grant_flower(db_session, 8710, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    outcomes, inventory, love, old_status = await _concurrent_flowers(
        db_session,
        user_a=8710,
        user_b=8711,
        old_gift_id=old_gift.id,
        old_gift_sender=8711,
        old_gift_receiver=8710,
        resolution="accept",
    )
    assert outcomes == [("SENT", outcomes[0][1]), ("ACCEPT", 25)]
    # Acceptance does not refund, so the send's consumption is the net effect.
    assert inventory == 0
    assert love == 25
    assert old_status == FlowerGiftStatus.ACCEPTED
    pending = await LoveService.get_pending_gifts_for_marriage(db_session, marriage.id)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_race_send_flower_versus_flower_invalidate(db_session):
    """Flower send racing with an invalidation (refund) on the same couple."""
    marriage = await _mk_marriage(db_session, 8720, 8721, love_score=20)
    old_gift = FlowerGift(
        marriage_id=marriage.id,
        sender_id=8720,
        receiver_id=8721,
        gift_date="2026-10-01",
        status=FlowerGiftStatus.PENDING,
        love_delta=5,
    )
    db_session.add(old_gift)
    await _grant_flower(db_session, 8720, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    outcomes, inventory, love, old_status = await _concurrent_flowers(
        db_session,
        user_a=8720,
        user_b=8721,
        old_gift_id=old_gift.id,
        old_gift_sender=8720,
        old_gift_receiver=8721,
        resolution="invalidate",
    )
    assert outcomes == [("SENT", outcomes[0][1]), ("INVALIDATE", 20)]
    # 1 granted - 1 consumed by the send + 1 refunded by the invalidation
    assert inventory == 1
    # Invalidation must never move Love.
    assert love == 20
    assert old_status == FlowerGiftStatus.INVALIDATED
    pending = await LoveService.get_pending_gifts_for_marriage(db_session, marriage.id)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_race_flower_refund_versus_concurrent_inventory_consumption(db_session):
    """A rejection refund racing with an unrelated Flower consumption.

    Inventory can never go negative and can never be refunded twice: the two
    orders are both legal, so only the final total is pinned.
    """
    marriage = await _mk_marriage(db_session, 8730, 8731, love_score=8)
    old_gift = FlowerGift(
        marriage_id=marriage.id,
        sender_id=8730,
        receiver_id=8731,
        gift_date="2026-10-01",
        status=FlowerGiftStatus.PENDING,
        love_delta=5,
    )
    db_session.add(old_gift)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()
    # The old gift already consumed 8730's only Flower -> inventory is 0.

    maker = get_async_sessionmaker()
    now = datetime(2026, 10, 3, 21, 30, 0, tzinfo=timezone.utc)

    async def do_reject():
        async with maker() as s:
            try:
                res = await LoveService.reject_flower(s, old_gift.id, 8731, now=now)
                await s.commit()
                return res.new_love
            except LoveServiceError:
                await s.rollback()
                return None

    async def do_consume():
        async with maker() as s:
            try:
                await ItemService.consume(s, 8730, "flower", 1)
                await s.commit()
                return True
            except Exception:
                await s.rollback()
                return False

    love, consumed = await asyncio.gather(do_reject(), do_consume())

    db_session.expunge_all()
    inventory = await ItemService.get_total(db_session, 8730, "flower")
    # Rejection always refunds exactly 1; the external consume either took it
    # (final 0) or found nothing and failed (final 1). Never negative, never 2.
    assert inventory in (0, 1)
    assert inventory == (0 if consumed else 1)
    assert love == 7
    refreshed = await LoveService.get_gift(db_session, old_gift.id)
    assert refreshed is not None and refreshed.status == FlowerGiftStatus.REJECTED


@pytest.mark.asyncio
async def test_reject_flower_atomicity_rolls_back_if_refund_fails(
    db_session, monkeypatch
):
    marriage = await _mk_marriage(db_session, 8650, 8651, love_score=10)
    await _grant_flower(db_session, 8650, 1)
    await _configure_love_gain(db_session, 5)
    await db_session.commit()

    t0 = datetime(2026, 10, 3, 20, 15, 0, tzinfo=timezone.utc)
    gift = await LoveService.send_flower(db_session, 8650, 8651, now=t0)
    await db_session.commit()

    async def _fail_refund(*args, **kwargs):
        raise RuntimeError("Simulated refund failure")

    monkeypatch.setattr(LoveService, "_refund_flower", _fail_refund)

    with pytest.raises(RuntimeError, match="Simulated refund failure"):
        await LoveService.reject_flower(
            db_session, gift.id, 8651, now=t0 + timedelta(seconds=10)
        )
    await db_session.commit()

    # Gift remains PENDING, Love remains 10, inventory remains 0
    refreshed_gift = await LoveService.get_gift(db_session, gift.id)
    await db_session.refresh(marriage)
    assert refreshed_gift is not None and refreshed_gift.status == FlowerGiftStatus.PENDING
    assert marriage.love_score == 10
    assert await ItemService.get_total(db_session, 8650, "flower") == 0

