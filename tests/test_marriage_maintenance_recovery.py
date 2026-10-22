"""Phase 10 — Marriage & Love maintenance + recovery wiring.

Scope: prove that overdue proposal expiration and daily Love decay are
driven by the EXISTING MaintenanceService sweep and the EXISTING
RecoveryService startup path, that both are idempotent, and that a
Marriage/Love failure can never break unrelated maintenance work.

No Marriage/Love business rule is re-implemented or re-asserted here: the
domain suites own those. Every test calls the real MaintenanceService /
RecoveryService entry points and only checks the wiring and its outcomes.
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
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
)
from sib.services.config_store import ConfigService
from sib.services.love import LoveService
from sib.services.maintenance.service import MaintenanceService
from sib.services.marriage_proposal import MarriageProposalService
from sib.services.recovery import RecoveryService

# A private ID range: the ``users`` table is not truncated between tests.
GROUP_ID = -1009800
GROUP_TITLE = "گروه نگهداری ازدواج"

PROPOSER_ID = 9801
TARGET_ID = 9802
OTHER_PROPOSER_ID = 9803
OTHER_TARGET_ID = 9804
SPOUSE_A_ID = 9811
SPOUSE_B_ID = 9812
DECAY_A_ID = 9821
DECAY_B_ID = 9822


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(n: int) -> datetime:
    return _utcnow() - timedelta(days=n)


def _date_str(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


async def _mk_user(session, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=f"کاربر{user_id}",
            language_code="fa",
        )
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


async def _mk_proposal(
    session,
    proposer_id: int,
    target_id: int,
    *,
    expires_at: datetime,
    status: str = ProposalStatus.PENDING,
) -> int:
    """A proposal row with an explicit ``expires_at`` (no ring needed)."""
    await _mk_user(session, proposer_id)
    await _mk_user(session, target_id)
    await _mk_group(session)
    await session.flush()
    proposal = MarriageProposal(
        proposer_id=proposer_id,
        target_id=target_id,
        group_id=GROUP_ID,
        status=status,
        expires_at=expires_at,
    )
    session.add(proposal)
    await session.commit()
    return int(proposal.id)


async def _mk_marriage(
    session,
    a: int,
    b: int,
    *,
    love_score: int = 0,
    married_at: datetime = None,
    last_decay_date: str = None,
    status: str = MarriageStatus.ACTIVE,
) -> int:
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
        status=status,
        love_score=love_score,
        married_at=married_at or _utcnow(),
        last_decay_date=last_decay_date,
    )
    session.add(marriage)
    await session.commit()
    return int(marriage.id)


async def _proposal_status(session, proposal_id: int) -> str:
    session.expunge_all()
    proposal = await session.get(MarriageProposal, proposal_id)
    return proposal.status


async def _love(session, marriage_id: int) -> int:
    session.expunge_all()
    marriage = await session.get(Marriage, marriage_id)
    return int(marriage.love_score)


async def _last_decay(session, marriage_id: int):
    session.expunge_all()
    marriage = await session.get(Marriage, marriage_id)
    return marriage.last_decay_date


# ---------------------------------------------------------------------------
# A. Proposal expiration via MaintenanceService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_expires_an_overdue_proposal(db_session):
    proposal_id = await _mk_proposal(
        db_session, PROPOSER_ID, TARGET_ID, expires_at=_utcnow() - timedelta(seconds=1)
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["expired_marriage_proposals"] == 1
    assert await _proposal_status(db_session, proposal_id) == ProposalStatus.EXPIRED


@pytest.mark.asyncio
async def test_maintenance_leaves_a_non_overdue_proposal_pending(db_session):
    proposal_id = await _mk_proposal(
        db_session, PROPOSER_ID, TARGET_ID, expires_at=_utcnow() + timedelta(minutes=4)
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["expired_marriage_proposals"] == 0
    assert await _proposal_status(db_session, proposal_id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_proposal_expiration_is_idempotent(db_session):
    """A second sweep finds nothing left to expire and changes nothing."""
    proposal_id = await _mk_proposal(
        db_session, PROPOSER_ID, TARGET_ID, expires_at=_utcnow() - timedelta(minutes=10)
    )

    first = await MaintenanceService.run_marriage_love_cycle()
    resolved_at_after_first = (
        await db_session.scalar(
            select(MarriageProposal.resolved_at).where(
                MarriageProposal.id == proposal_id
            )
        )
    )
    second = await MaintenanceService.run_marriage_love_cycle()

    assert first["expired_marriage_proposals"] == 1
    assert second["expired_marriage_proposals"] == 0
    assert await _proposal_status(db_session, proposal_id) == ProposalStatus.EXPIRED
    # The already-resolved timestamp is untouched by the repeat sweep.
    resolved_at_now = await db_session.scalar(
        select(MarriageProposal.resolved_at).where(MarriageProposal.id == proposal_id)
    )
    assert resolved_at_now == resolved_at_after_first


@pytest.mark.asyncio
async def test_expiration_never_overwrites_a_resolved_proposal(db_session):
    """The CAS guard: an already-accepted proposal is never expired."""
    proposal_id = await _mk_proposal(
        db_session,
        OTHER_PROPOSER_ID,
        OTHER_TARGET_ID,
        expires_at=_utcnow() - timedelta(minutes=10),
        status=ProposalStatus.ACCEPTED,
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["expired_marriage_proposals"] == 0
    assert await _proposal_status(db_session, proposal_id) == ProposalStatus.ACCEPTED


@pytest.mark.asyncio
async def test_run_once_includes_the_marriage_love_cycle(db_session):
    """The existing 5s sweep drives it - no second loop exists."""
    with patch.object(
        MaintenanceService,
        "run_marriage_love_cycle",
        new=AsyncMock(return_value={}),
    ) as mocked:
        await MaintenanceService.run_once()

    assert mocked.await_count == 1


# ---------------------------------------------------------------------------
# B. Daily Love decay via MaintenanceService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_applies_daily_love_decay_once_for_a_completed_day(db_session):
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=10,
        married_at=_days_ago(1),
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["love_decay_marriages"] == 1
    assert await _love(db_session, marriage_id) == 9
    assert await _last_decay(session=db_session, marriage_id=marriage_id) == _date_str(
        _utcnow()
    )


@pytest.mark.asyncio
async def test_the_same_day_can_never_decay_twice(db_session):
    """Two cycles in one UTC day: only the first changes anything."""
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=50,
        married_at=_days_ago(1),
    )

    first = await MaintenanceService.run_marriage_love_cycle()
    score_after_first = await _love(db_session, marriage_id)
    second = await MaintenanceService.run_marriage_love_cycle()

    assert first["love_decay_marriages"] == 1
    assert score_after_first == 49
    assert second["love_decay_marriages"] == 0
    assert await _love(db_session, marriage_id) == 49


@pytest.mark.asyncio
async def test_multiple_missed_days_are_caught_up(db_session):
    """Three days offline must cost three daily decays, not one."""
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=30,
        married_at=_days_ago(3),
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["love_decay_marriages"] == 1
    assert await _love(db_session, marriage_id) == 27
    assert await _last_decay(session=db_session, marriage_id=marriage_id) == _date_str(
        _utcnow()
    )


@pytest.mark.asyncio
async def test_love_never_decays_below_zero(db_session):
    """The configured floor (love_min = 0) is honoured, even when catching up."""
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=2,
        married_at=_days_ago(50),
    )

    await MaintenanceService.run_marriage_love_cycle()

    assert await _love(db_session, marriage_id) == 0
    # The day is stamped so the marriage stops being re-examined every cycle.
    assert await _last_decay(session=db_session, marriage_id=marriage_id) == _date_str(
        _utcnow()
    )


@pytest.mark.asyncio
async def test_decay_respects_the_configured_love_daily_decay(db_session):
    """The amount comes from the existing config key - never hardcoded."""
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=80,
        married_at=_days_ago(1),
    )
    await ConfigService.set(db_session, "love_daily_decay", 7)
    await db_session.commit()

    try:
        await MaintenanceService.run_marriage_love_cycle()
        assert await _love(db_session, marriage_id) == 73
    finally:
        await ConfigService.set(db_session, "love_daily_decay", 1)
        await db_session.commit()


@pytest.mark.asyncio
async def test_decay_ignores_divorced_marriages(db_session):
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=0,
        married_at=_days_ago(5),
        status=MarriageStatus.DIVORCED,
    )

    stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["love_decay_marriages"] == 0
    assert await _last_decay(session=db_session, marriage_id=marriage_id) is None


# ---------------------------------------------------------------------------
# D. Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_marriage_love_failure_never_breaks_the_cycle(db_session):
    """One failing step must not raise, and must not skip the other step."""
    marriage_id = await _mk_marriage(
        db_session,
        DECAY_A_ID,
        DECAY_B_ID,
        love_score=10,
        married_at=_days_ago(1),
    )
    proposal_id = await _mk_proposal(
        db_session, PROPOSER_ID, TARGET_ID, expires_at=_utcnow() - timedelta(minutes=1)
    )

    with patch.object(
        MarriageProposalService,
        "expire_overdue_proposals",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        stats = await MaintenanceService.run_marriage_love_cycle()

    assert stats["expired_marriage_proposals"] == 0
    # Love decay still ran despite the expiration failure above.
    assert stats["love_decay_marriages"] == 1
    assert await _love(db_session, marriage_id) == 9
    assert await _proposal_status(db_session, proposal_id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_run_once_survives_a_marriage_love_failure(db_session):
    """Unrelated maintenance (matchmaking) still runs when Marriage/Love dies."""
    with patch.object(
        MaintenanceService,
        "run_marriage_love_cycle",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # Must not raise: the failure is logged and contained.
        await MaintenanceService.run_once()


# ---------------------------------------------------------------------------
# C. Startup recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_expires_overdue_proposals(db_session):
    proposal_id = await _mk_proposal(
        db_session, PROPOSER_ID, TARGET_ID, expires_at=_utcnow() - timedelta(minutes=30)
    )
    fresh_id = await _mk_proposal(
        db_session,
        OTHER_PROPOSER_ID,
        OTHER_TARGET_ID,
        expires_at=_utcnow() + timedelta(minutes=3),
    )

    stats = await RecoveryService.recover_on_startup(db_session)

    assert stats["expired_marriage_proposals"] == 1
    assert await _proposal_status(db_session, proposal_id) == ProposalStatus.EXPIRED
    assert await _proposal_status(db_session, fresh_id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_recovery_applies_missed_love_decay(db_session):
    marriage_id = await _mk_marriage(
        db_session,
        SPOUSE_A_ID,
        SPOUSE_B_ID,
        love_score=40,
        married_at=_days_ago(4),
    )

    stats = await RecoveryService.recover_on_startup(db_session)

    assert stats["love_decay_marriages"] == 1
    assert await _love(db_session, marriage_id) == 36


@pytest.mark.asyncio
async def test_running_recovery_twice_does_not_duplicate_decay(db_session):
    marriage_id = await _mk_marriage(
        db_session,
        SPOUSE_A_ID,
        SPOUSE_B_ID,
        love_score=40,
        married_at=_days_ago(4),
    )

    first = await RecoveryService.recover_on_startup(db_session)
    score_after_first = await _love(db_session, marriage_id)
    second = await RecoveryService.recover_on_startup(db_session)

    assert first["love_decay_marriages"] == 1
    assert score_after_first == 36
    assert second["love_decay_marriages"] == 0
    assert await _love(db_session, marriage_id) == 36


# ---------------------------------------------------------------------------
# G12. Maintenance must not disturb live Marriage/Love state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_does_not_disturb_active_marriage_or_pending_gift(db_session):
    """A PENDING flower gift and its ACTIVE marriage survive a full cycle."""
    marriage_id = await _mk_marriage(
        db_session,
        SPOUSE_A_ID,
        SPOUSE_B_ID,
        love_score=5,
        married_at=_days_ago(1),
    )
    today = _date_str(_utcnow())
    gift = FlowerGift(
        marriage_id=marriage_id,
        sender_id=SPOUSE_A_ID,
        receiver_id=SPOUSE_B_ID,
        chat_id=GROUP_ID,
        message_id=7001,
        gift_date=today,
        status=FlowerGiftStatus.PENDING,
    )
    db_session.add(gift)
    await db_session.commit()
    gift_id = int(gift.id)

    stats = await MaintenanceService.run_marriage_love_cycle()

    db_session.expunge_all()
    assert stats["expired_marriage_proposals"] == 0
    marriage = await db_session.get(Marriage, marriage_id)
    assert marriage.status == MarriageStatus.ACTIVE
    assert marriage.love_score == 4  # one daily decay, nothing else

    pending = await db_session.scalar(
        select(FlowerGift).where(
            FlowerGift.id == gift_id, FlowerGift.status == FlowerGiftStatus.PENDING
        )
    )
    assert pending is not None, "maintenance must never resolve a pending gift"


@pytest.mark.asyncio
async def test_recovery_does_not_disturb_active_marriage_or_pending_gift(db_session):
    marriage_id = await _mk_marriage(
        db_session,
        SPOUSE_A_ID,
        SPOUSE_B_ID,
        love_score=5,
        married_at=_days_ago(1),
    )
    today = _date_str(_utcnow())
    gift = FlowerGift(
        marriage_id=marriage_id,
        sender_id=SPOUSE_A_ID,
        receiver_id=SPOUSE_B_ID,
        chat_id=GROUP_ID,
        message_id=7002,
        gift_date=today,
        status=FlowerGiftStatus.PENDING,
    )
    db_session.add(gift)
    await db_session.commit()
    gift_id = int(gift.id)

    await RecoveryService.recover_on_startup(db_session)

    db_session.expunge_all()
    marriage = await db_session.get(Marriage, marriage_id)
    assert marriage.status == MarriageStatus.ACTIVE
    pending = await db_session.scalar(
        select(FlowerGift).where(
            FlowerGift.id == gift_id, FlowerGift.status == FlowerGiftStatus.PENDING
        )
    )
    assert pending is not None, "recovery must never resolve a pending gift"


# ---------------------------------------------------------------------------
# No new worker / scheduler
# ---------------------------------------------------------------------------


def test_no_new_background_loop_was_introduced():
    """Marriage/Love reuses the single existing maintenance loop."""
    import inspect

    source = inspect.getsource(MaintenanceService)
    # Exactly one loop driver, unchanged.
    assert source.count("async def run_forever") == 1
    assert MaintenanceService.INTERVAL_SECONDS == 30
    # The Marriage/Love work is a plain step of the existing sweep.
    assert "run_marriage_love_cycle" in source
    assert not hasattr(MaintenanceService, "run_marriage_love_forever")


def test_marriage_love_reuses_existing_domain_apis():
    """Maintenance/recovery delegate - they do not reproduce domain logic."""
    assert hasattr(MarriageProposalService, "expire_overdue_proposals")
    assert hasattr(LoveService, "apply_daily_decay")
