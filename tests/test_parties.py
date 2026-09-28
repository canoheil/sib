import os
"""Tests for Party Service, One-Active-Session enforcement, and team modes."""

import asyncio
import uuid
import pytest

from sib.tests.conftest import requires_row_locks
from sqlalchemy import select
from sib.core.config import settings
from sib.core.errors import (
    ActiveSessionExistsException,
    PartyFullException,
    PartyNotFoundException,
    InvalidActionException,
)
from sib.database.models import User, Group, Game, Party, PartyMember, PartyStatus, GroupStats
from sib.services.party import PartyService
from sib.services.session import SessionService


async def create_test_user(session, name: str) -> User:
    uid = int(uuid.uuid4().int % 1000000000)
    user = User(id=uid, first_name=name, username=f"{name}_{uid}")
    session.add(user)
    await session.commit()
    return user


async def create_test_group(session, title: str) -> Group:
    gid = -int(uuid.uuid4().int % 1000000000)
    group = Group(id=gid, title=title)
    session.add(group)
    stats = GroupStats(group_id=gid)
    session.add(stats)
    await session.commit()
    return group


@pytest.mark.asyncio
async def test_create_solo_party(db_session):
    """Solo party should have max_players=1 and status immediately FULL."""
    user = await create_test_user(db_session, "علی")
    group = await create_test_group(db_session, "گروه تستی ۱")

    party = await PartyService.create_party(
        session=db_session,
        group_id=group.id,
        creator_user_id=user.id,
        game_code="rock_paper_scissors",
        mode="solo",
    )
    await db_session.commit()

    assert party is not None
    assert party.mode == "solo"
    assert party.max_players == 1
    assert party.current_players_count == 1
    assert party.status == PartyStatus.FULL
    assert party.is_full is True
    assert len(party.members) == 1
    assert party.members[0].user_id == user.id


@pytest.mark.asyncio
async def test_create_team_2v2_and_join(db_session):
    """2v2 team party starts with 1/2 players (OPEN), becomes FULL when 2nd user joins."""
    user1 = await create_test_user(db_session, "بازیکن ۱")
    user2 = await create_test_user(db_session, "بازیکن ۲")
    group = await create_test_group(db_session, "گروه کانکت فور")

    # Connect four supports 2v2
    party = await PartyService.create_party(
        session=db_session,
        group_id=group.id,
        creator_user_id=user1.id,
        game_code="connect_four",
        mode="2v2",
    )
    await db_session.commit()

    assert party.status == PartyStatus.OPEN
    assert party.max_players == 2
    assert party.current_players_count == 1
    assert party.remaining_slots == 1

    # User 2 joins
    updated_party = await PartyService.join_party(db_session, party.id, user2.id)
    await db_session.commit()

    assert updated_party.current_players_count == 2
    assert updated_party.status == PartyStatus.FULL
    assert updated_party.is_full is True
    assert len(updated_party.members) == 2


@pytest.mark.asyncio
async def test_one_active_session_rule(db_session):
    """User with an active party cannot create another party or join another party."""
    user = await create_test_user(db_session, "کاربر فعال")
    user_other = await create_test_user(db_session, "سازنده دیگر")
    group = await create_test_group(db_session, "گروه تست سشن")

    # User creates a party
    party1 = await PartyService.create_party(
        session=db_session,
        group_id=group.id,
        creator_user_id=user.id,
        game_code="rock_paper_scissors",
        mode="solo",
    )
    await db_session.commit()

    assert await SessionService.has_active_session(db_session, user.id) is True

    # Attempting to create a second party must raise ActiveSessionExistsException
    with pytest.raises(ActiveSessionExistsException):
        await PartyService.create_party(
            session=db_session,
            group_id=group.id,
            creator_user_id=user.id,
            game_code="connect_four",
            mode="solo",
        )

    # Other user creates an open party
    party2 = await PartyService.create_party(
        session=db_session,
        group_id=group.id,
        creator_user_id=user_other.id,
        game_code="connect_four",
        mode="2v2",
    )
    await db_session.commit()

    # Active user attempting to join party2 must also be blocked
    with pytest.raises(ActiveSessionExistsException):
        await PartyService.join_party(db_session, party2.id, user.id)


@pytest.mark.asyncio
async def test_party_full_rejection(db_session):
    """Attempting to join a full party raises PartyFullException."""
    u1 = await create_test_user(db_session, "A1")
    u2 = await create_test_user(db_session, "A2")
    u3 = await create_test_user(db_session, "A3")
    group = await create_test_group(db_session, "گروه تکمیل")

    party = await PartyService.create_party(db_session, group.id, u1.id, "connect_four", "2v2")
    await PartyService.join_party(db_session, party.id, u2.id)
    await db_session.commit()

    with pytest.raises(PartyFullException):
        await PartyService.join_party(db_session, party.id, u3.id)


@pytest.mark.asyncio
async def test_party_leave_and_reopen(db_session):
    """When a member leaves a full party, slots decrement and status returns to OPEN."""
    u1 = await create_test_user(db_session, "عضو ۱")
    u2 = await create_test_user(db_session, "عضو ۲")
    group = await create_test_group(db_session, "گروه ترک")

    party = await PartyService.create_party(db_session, group.id, u1.id, "battleship", "2v2")
    await PartyService.join_party(db_session, party.id, u2.id)
    await db_session.commit()

    # User 2 leaves
    updated_party, was_cancelled = await PartyService.leave_party(db_session, party.id, u2.id)
    await db_session.commit()

    assert was_cancelled is False
    assert updated_party.status == PartyStatus.OPEN
    assert updated_party.current_players_count == 1
    assert updated_party.remaining_slots == 1

    # User 2 is now free of active sessions
    assert await SessionService.has_active_session(db_session, u2.id) is False


@pytest.mark.asyncio
async def test_last_member_leave_cancels_party(db_session):
    """When the sole member leaves, party is automatically cancelled."""
    u = await create_test_user(db_session, "تنها بازیکن")
    group = await create_test_group(db_session, "گروه لغو خودکار")

    party = await PartyService.create_party(db_session, group.id, u.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    updated_party, was_cancelled = await PartyService.leave_party(db_session, party.id, u.id)
    await db_session.commit()

    assert was_cancelled is True
    assert updated_party is None

    # Check database status
    refreshed = await db_session.scalar(select(Party).where(Party.id == party.id))
    assert refreshed.status == PartyStatus.CANCELLED


@pytest.mark.asyncio
async def test_unsupported_mode_rejection(db_session):
    """Attempting to create a team party on a solo-only game raises InvalidActionException."""
    u = await create_test_user(db_session, "بازیکن دوز")
    group = await create_test_group(db_session, "گروه دوز")

    # Tic-tac-toe only supports solo
    with pytest.raises(InvalidActionException):
        await PartyService.create_party(
            session=db_session,
            group_id=group.id,
            creator_user_id=u.id,
            game_code="tic_tac_toe",
            mode="2v2",
        )


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_party_join_atomic(db_session):
    """Simulate two concurrent users attempting to fill the last party slot."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy.pool import NullPool
    from sib.core.config import settings

    u1 = await create_test_user(db_session, "سازنده رقابت")
    u2 = await create_test_user(db_session, "رقابت‌کننده الف")
    u3 = await create_test_user(db_session, "رقابت‌کننده ب")
    group = await create_test_group(db_session, "گروه تست همزمانی")

    # 2v2 party has 2 slots; u1 takes 1 slot, 1 slot remains
    party = await PartyService.create_party(db_session, group.id, u1.id, "connect_four", "2v2")
    await db_session.commit()

    test_database_url = os.getenv("sib_TEST_DATABASE_URL")
    if not test_database_url:
        raise RuntimeError("sib_TEST_DATABASE_URL is required")
    engine = create_async_engine(test_database_url, poolclass=NullPool, echo=False)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    results = []

    async def try_join(user_id: int):
        async with session_maker() as session:
            try:
                async with session.begin():
                    res = await PartyService.join_party(session, party.id, user_id)
                results.append(("SUCCESS", user_id))
            except Exception as e:
                results.append((type(e).__name__, user_id))

    # Run both simultaneously
    await asyncio.gather(
        try_join(u2.id),
        try_join(u3.id),
    )
    await engine.dispose()

    # Exactly one must succeed, and exactly one must fail with PartyFullException
    successes = [r for r in results if r[0] == "SUCCESS"]
    failures = [r for r in results if r[0] == "PartyFullException"]

    assert len(successes) == 1
    assert len(failures) == 1
