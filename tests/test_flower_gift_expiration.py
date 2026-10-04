"""Time-based expiration for PENDING FlowerGifts.

Scope: the missing piece found by the final Marriage + Love audit. A gift
nobody accepts or rejects is expired by MaintenanceService (periodic) and
RecoveryService (startup) once ``created_at + flower_gift_pending_timeout_seconds``
has passed, refunding exactly one Flower and never changing Love.

Nothing else about the Flower feature is touched: buy/sell/send/accept/reject,
the daily directional limit, the Love gain/penalty/clamp and the Telegram UI
are all unchanged and are covered by the existing suites.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from sib.database import (
    FlowerGift,
    FlowerGiftStatus,
    Group,
    Marriage,
    MarriageStatus,
    User,
)
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.love import LoveService
from sib.services.maintenance.service import MaintenanceService
from sib.services.recovery import RecoveryService

# A private ID range: the ``users`` table is not truncated between tests.
GROUP_ID = -1009830
GROUP_TITLE = "گروه انقضای گل"

SENDER_ID = 9831
RECEIVER_ID = 9832
OTHER_A_ID = 9841
OTHER_B_ID = 9842

# Fixed clock for the direct ``LoveService`` sweeps (they accept ``now``).
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _mk_user(session, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=f"کاربر{user_id}", language_code="fa")
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = GROUP_ID) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title=GROUP_TITLE)
        session.add(group)
        await session.flush()
    return group


async def _mk_marriage(session, a: int, b: int, *, love_score: int = 10) -> int:
    await _mk_user(session, a)
    await _mk_user(session, b)
    await _mk_group(session)
    await session.flush()
    marriage = Marriage(
        user1_id=min(a, b),
        user2_id=max(a, b),
        proposer_id=a,
        accepter_id=b,
        origin_group_id=GROUP_ID,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    session.add(marriage)
    await session.commit()
    return int(marriage.id)


async def _mk_gift(
    session,
    marriage_id: int,
    sender_id: int,
    receiver_id: int,
    *,
    created_at: datetime,
    status: str = FlowerGiftStatus.PENDING,
) -> int:
    """A FlowerGift whose ``created_at`` is set explicitly.

    ``created_at`` already exists (``TimestampMixin``) and is what the
    expiration deadline is derived from, so the test controls time by
    writing that column rather than by adding one.
    """
    gift = FlowerGift(
        marriage_id=marriage_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        chat_id=GROUP_ID,
        message_id=7000,
        gift_date=created_at.strftime("%Y-%m-%d"),
        status=status,
        love_delta=0,
        created_at=created_at,
    )
    session.add(gift)
    await session.commit()
    return int(gift.id)


async def _gift_status(session, gift_id: int) -> str:
    session.expunge_all()
    gift = await session.get(FlowerGift, gift_id)
    return gift.status


async def _flower_total(session, user_id: int) -> int:
    return await ItemService.get_total(session, user_id, "flower")


async def _love(session, marriage_id: int) -> int:
    session.expunge_all()
    marriage = await session.get(Marriage, marriage_id)
    return int(marriage.love_score)


# ---------------------------------------------------------------------------
# Schema reuse (requirements 2 and 7)
# ---------------------------------------------------------------------------


def test_expiration_derives_the_deadline_from_created_at_no_new_column():
    """No migration: the deadline is ``created_at`` + configured lifetime."""
    assert hasattr(FlowerGift, "created_at"), "created_at must exist"
    # Deliberately NOT introduced: an explicit expiry column was unnecessary.
    assert not hasattr(FlowerGift, "expires_at")
    assert not hasattr(FlowerGift, "expires_at_seconds")


@pytest.mark.asyncio
async def test_the_default_lifetime_is_configured_not_hardcoded(db_session):
    """The duration comes from ConfigService, with a documented default."""
    configured = await ConfigService.get(
        db_session, "flower_gift_pending_timeout_seconds"
    )
    assert configured == 300  # mirrors marriage_proposal_timeout_seconds

    # The service reads the same key rather than baking in a number.
    await ConfigService.set(db_session, "flower_gift_pending_timeout_seconds", 42)
    await db_session.commit()
    try:
        assert await LoveService._get_pending_gift_timeout_seconds(db_session) == 42
    finally:
        await ConfigService.set(
            db_session, "flower_gift_pending_timeout_seconds", 300
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_a_non_positive_lifetime_falls_back_to_the_default(db_session):
    await ConfigService.set(db_session, "flower_gift_pending_timeout_seconds", 0)
    await db_session.commit()
    try:
        # Matches how create_proposal treats a non-positive timeout.
        seconds = await LoveService._get_pending_gift_timeout_seconds(db_session)
        assert seconds == 300
    finally:
        await ConfigService.set(
            db_session, "flower_gift_pending_timeout_seconds", 300
        )
        await db_session.commit()


# ---------------------------------------------------------------------------
# Core expiration behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_gift_expires_after_the_configured_duration(db_session):
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    gift_id = await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=301),  # 300s lifetime has passed
    )

    expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert len(expired) == 1
    assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.EXPIRED


@pytest.mark.asyncio
async def test_a_gift_before_its_deadline_stays_pending(db_session):
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    gift_id = await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=299),  # one second short
    )

    expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert expired == []
    assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_expiration_refunds_exactly_one_flower(db_session):
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=600),
    )
    # The sender spent their only Flower when the gift was created.
    assert await _flower_total(db_session, SENDER_ID) == 0

    await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert await _flower_total(db_session, SENDER_ID) == 1


@pytest.mark.asyncio
async def test_expiration_does_not_change_love(db_session):
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID, love_score=17)
    await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=600),
    )

    results = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert len(results) == 1
    assert results[0].love_delta == 0
    assert results[0].flower_refunded is True
    assert await _love(db_session, marriage_id) == 17


@pytest.mark.asyncio
async def test_already_resolved_gifts_are_never_touched(db_session):
    """ACCEPTED / REJECTED / INVALIDATED / EXPIRED rows are out of scope."""
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    long_ago = NOW - timedelta(days=10)
    ids = {
        status: await _mk_gift(
            db_session,
            marriage_id,
            SENDER_ID,
            RECEIVER_ID,
            created_at=long_ago,
            status=status,
        )
        for status in (
            FlowerGiftStatus.ACCEPTED,
            FlowerGiftStatus.REJECTED,
            FlowerGiftStatus.INVALIDATED,
            FlowerGiftStatus.EXPIRED,
        )
    }

    expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert expired == []
    for status, gift_id in ids.items():
        assert await _gift_status(db_session, gift_id) == status


@pytest.mark.asyncio
async def test_expiration_is_idempotent(db_session):
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=600),
    )

    first = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()
    second = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert len(first) == 1
    assert second == []
    # Still exactly one refund - a repeat sweep never double-refunds.
    assert await _flower_total(db_session, SENDER_ID) == 1


@pytest.mark.asyncio
async def test_the_configured_duration_is_respected(db_session):
    """A different configured lifetime changes the deadline."""
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    gift_id = await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=120),  # overdue only if TTL <= 120
    )
    await ConfigService.set(db_session, "flower_gift_pending_timeout_seconds", 60)
    await db_session.commit()

    try:
        expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
        await db_session.commit()
        assert len(expired) == 1
        assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.EXPIRED
    finally:
        await ConfigService.set(
            db_session, "flower_gift_pending_timeout_seconds", 300
        )
        await db_session.commit()


# ---------------------------------------------------------------------------
# Racing against a concurrent resolution - no double refund
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_concurrent_accept_wins_and_is_never_refunded_by_expiry(db_session):
    """Accept-then-expire must not give back the Flower on top of the Love."""
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID, love_score=10)
    await ItemService.grant(db_session, SENDER_ID, "flower", 1)
    await db_session.commit()

    sent = await LoveService.send_flower(
        db_session, SENDER_ID, RECEIVER_ID, now=NOW
    )
    gift_id = int(sent.gift_id)
    await db_session.commit()
    # Back-date it so it is also overdue for the sweep.
    await db_session.execute(
        select(FlowerGift).where(FlowerGift.id == gift_id)
    )
    gift = await db_session.get(FlowerGift, gift_id)
    gift.created_at = NOW - timedelta(seconds=600)
    await db_session.commit()

    # The receiver accepts at the same moment the sweep runs.
    await LoveService.accept_flower(db_session, gift_id, RECEIVER_ID, now=NOW)
    await db_session.commit()

    expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert expired == [], "an accepted gift must never be expired"
    assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.ACCEPTED
    # Double-refund would show 1 here; the Flower stayed consumed.
    assert await _flower_total(db_session, SENDER_ID) == 0
    assert await _love(db_session, marriage_id) == 12  # gain of 2, unchanged rule


@pytest.mark.asyncio
async def test_a_concurrent_invalidate_refunds_exactly_once(db_session):
    """Invalidate-then-expire must not refund the Flower a second time."""
    await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    await ItemService.grant(db_session, SENDER_ID, "flower", 1)
    await db_session.commit()

    sent = await LoveService.send_flower(
        db_session, SENDER_ID, RECEIVER_ID, now=NOW
    )
    gift_id = int(sent.gift_id)
    gift = await db_session.get(FlowerGift, gift_id)
    gift.created_at = NOW - timedelta(seconds=600)
    await db_session.commit()

    # Another path resolves it first and refunds.
    await LoveService.invalidate_gift(db_session, gift_id, now=NOW)
    await db_session.commit()
    assert await _flower_total(db_session, SENDER_ID) == 1

    expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert expired == []
    # Exactly one refund in total - not two.
    assert await _flower_total(db_session, SENDER_ID) == 1
    assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.INVALIDATED


@pytest.mark.asyncio
async def test_sweep_skips_a_gift_resolved_between_selection_and_expiry(db_session):
    """The CAS guard, exercised as a real interleaving.

    The sweep selects the gift while it is still PENDING, and only then does
    a concurrent transaction resolve it. The conditional
    ``UPDATE ... WHERE status = 'PENDING'`` must claim zero rows, so the
    expiration is abandoned instead of refunding a second time.
    """
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    gift_id = await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=600),
    )
    original = LoveService.expire_gift.__func__

    async def _racing_expire(cls, session, target_id, *, now=None):
        # A concurrent accept/invalidate lands between the sweep's SELECT
        # and its UPDATE claim.
        await LoveService.invalidate_gift(session, target_id, now=now)
        return await original(cls, session, target_id, now=now)

    with patch.object(LoveService, "expire_gift", classmethod(_racing_expire)):
        expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    assert expired == [], "the losing expiration must be abandoned, not raised"
    assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.INVALIDATED
    # Refunded once by the concurrent resolution - never twice.
    assert await _flower_total(db_session, SENDER_ID) == 1
    assert await _love(db_session, marriage_id) == 10


@pytest.mark.asyncio
async def test_one_failing_gift_does_not_roll_back_the_others(db_session):
    """Each gift is expired in its own savepoint, so one failure is contained."""
    marriage_id = await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    first = await _mk_gift(
        db_session,
        marriage_id,
        SENDER_ID,
        RECEIVER_ID,
        created_at=NOW - timedelta(seconds=600),
    )
    second = await _mk_gift(
        db_session,
        marriage_id,
        RECEIVER_ID,
        SENDER_ID,
        created_at=NOW - timedelta(seconds=601),
    )
    original = LoveService.expire_gift.__func__
    seen = []

    async def _flaky_expire(cls, session, target_id, *, now=None):
        seen.append(target_id)
        if len(seen) == 1:
            raise RuntimeError("transient failure on the first gift")
        return await original(cls, session, target_id, now=now)

    with patch.object(LoveService, "expire_gift", classmethod(_flaky_expire)):
        expired = await LoveService.expire_overdue_gifts(db_session, now=NOW)
    await db_session.commit()

    # Ascending id order, so the first one is the one that blew up - and the
    # sweep walked past it instead of aborting.
    assert seen == [first, second]
    assert len(expired) == 1
    assert int(expired[0].gift_id) == second
    # The poisoned gift stays PENDING and is retried on the next sweep.
    assert await _gift_status(db_session, first) == FlowerGiftStatus.PENDING
    assert await _gift_status(db_session, second) == FlowerGiftStatus.EXPIRED


# ---------------------------------------------------------------------------
# MaintenanceService wiring (no new worker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_expires_overdue_pending_gifts(db_session):
    marriage_id = await _mk_marriage(db_session, OTHER_A_ID, OTHER_B_ID)
    overdue = await _mk_gift(
        db_session,
        marriage_id,
        OTHER_A_ID,
        OTHER_B_ID,
        created_at=_utcnow() - timedelta(seconds=600),
    )
    fresh = await _mk_gift(
        db_session,
        marriage_id,
        OTHER_B_ID,
        OTHER_A_ID,
        created_at=_utcnow(),
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["expired_flower_gifts"] == 1
    assert await _gift_status(db_session, overdue) == FlowerGiftStatus.EXPIRED
    assert await _gift_status(db_session, fresh) == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_maintenance_survives_a_flower_expiration_failure(db_session):
    """A failing flower step must not break the rest of the cycle."""
    marriage_id = await _mk_marriage(db_session, OTHER_A_ID, OTHER_B_ID)
    await _mk_gift(
        db_session,
        marriage_id,
        OTHER_A_ID,
        OTHER_B_ID,
        created_at=_utcnow() - timedelta(seconds=600),
    )

    with patch.object(
        LoveService,
        "expire_overdue_gifts",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["expired_flower_gifts"] == 0
    # The unrelated Marriage/Love steps still ran (values are just 0 here).
    assert "expired_marriage_proposals" in stats
    assert "love_decay_marriages" in stats


# ---------------------------------------------------------------------------
# RecoveryService wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_expires_overdue_pending_gifts(db_session):
    marriage_id = await _mk_marriage(db_session, OTHER_A_ID, OTHER_B_ID)
    overdue = await _mk_gift(
        db_session,
        marriage_id,
        OTHER_A_ID,
        OTHER_B_ID,
        created_at=_utcnow() - timedelta(seconds=600),
    )
    fresh = await _mk_gift(
        db_session,
        marriage_id,
        OTHER_B_ID,
        OTHER_A_ID,
        created_at=_utcnow(),
    )

    stats = await RecoveryService.recover_on_startup(db_session)

    assert stats["expired_flower_gifts"] == 1
    assert await _gift_status(db_session, overdue) == FlowerGiftStatus.EXPIRED
    assert await _gift_status(db_session, fresh) == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_running_recovery_twice_does_not_double_refund(db_session):
    marriage_id = await _mk_marriage(db_session, OTHER_A_ID, OTHER_B_ID)
    await _mk_gift(
        db_session,
        marriage_id,
        OTHER_A_ID,
        OTHER_B_ID,
        created_at=_utcnow() - timedelta(seconds=600),
    )

    first = await RecoveryService.recover_on_startup(db_session)
    total_after_first = await _flower_total(db_session, OTHER_A_ID)
    second = await RecoveryService.recover_on_startup(db_session)

    assert first["expired_flower_gifts"] == 1
    assert total_after_first == 1
    assert second["expired_flower_gifts"] == 0
    assert await _flower_total(db_session, OTHER_A_ID) == 1


@pytest.mark.asyncio
async def test_recovery_leaves_working_state_alone(db_session):
    """A healthy pending gift and an ACTIVE marriage survive recovery."""
    marriage_id = await _mk_marriage(
        db_session, OTHER_A_ID, OTHER_B_ID, love_score=5
    )
    gift_id = await _mk_gift(
        db_session,
        marriage_id,
        OTHER_A_ID,
        OTHER_B_ID,
        created_at=_utcnow(),
    )

    stats = await RecoveryService.recover_on_startup(db_session)

    assert stats["expired_flower_gifts"] == 0
    assert await _gift_status(db_session, gift_id) == FlowerGiftStatus.PENDING
    db_session.expunge_all()
    marriage = await db_session.get(Marriage, marriage_id)
    assert marriage.status == MarriageStatus.ACTIVE
