"""Concurrency and stress tests for race condition protection."""

import asyncio
import pytest
from goje.core.errors import PartyFullException
from goje.services.party import PartyService
from goje.tests.test_parties import create_test_user, create_test_group
from goje.database.connection import get_session


@pytest.mark.asyncio
async def test_high_concurrency_party_join_atomic(db_session):
    """
    Stress test: 10 concurrent users race to join the last remaining slot in a 2v2 party.
    Exactly 1 user must succeed; all others must be rejected with PartyFullException.
    The party member count must strictly equal 2.
    """
    g = await create_test_group(db_session, "گروه تست همروندی شدید")
    creator = await create_test_user(db_session, "سازنده")
    party = await PartyService.create_party(db_session, g.id, creator.id, "connect_four", "2v2")
    await db_session.commit()

    # Create 10 competing users
    competitors = [await create_test_user(db_session, f"رقابت‌کننده {i}") for i in range(10)]
    await db_session.commit()

    successes = []
    failures = []

    async def try_join(user_id: int):
        async with get_session() as session:
            try:
                await PartyService.join_party(session, party.id, user_id)
                await session.commit()
                return True
            except PartyFullException:
                await session.rollback()
                return False

    # Execute all 10 joins concurrently
    results = await asyncio.gather(*(try_join(u.id) for u in competitors), return_exceptions=True)

    success_count = sum(1 for r in results if r is True)
    failure_count = sum(1 for r in results if r is False)

    # Exactly 1 can take the last slot
    assert success_count == 1
    assert failure_count == 9

    # Verify database state in a fresh session
    async with get_session() as verify_session:
        from sqlalchemy.orm import selectinload
        from sqlalchemy import select
        from goje.database.models import Party
        party_refreshed = await verify_session.scalar(
            select(Party).where(Party.id == party.id).options(selectinload(Party.members))
        )
        assert len(party_refreshed.members) == 2
