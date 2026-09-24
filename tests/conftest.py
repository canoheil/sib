"""Pytest fixtures and configuration."""

import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sib.core.config import settings
from sib.database.base import Base
from sib.database.models import Game, SponsorChannel, ChannelType
from sib.database.connection import seed_default_data


from sqlalchemy.pool import NullPool

@pytest.fixture(scope="session")
def test_settings():
    """Fixture providing settings configured for testing."""
    return settings


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Provides a clean transactional database session with test tables cleaned before each test."""
    test_database_url = os.getenv("sib_TEST_DATABASE_URL")
    if not test_database_url:
        raise RuntimeError(
            "sib_TEST_DATABASE_URL is required. "
            "Refusing to run tests against production database."
        )

    if "sibcom_sib_test" not in test_database_url:
        raise RuntimeError(
            "sib_TEST_DATABASE_URL does not point to the expected test database."
        )

    engine = create_async_engine(
        test_database_url,
        poolclass=NullPool,
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Make get_session() use the isolated test database.
    import sib.database.connection as db_connection
    previous_engine = db_connection._async_engine
    previous_sessionmaker = db_connection._async_sessionmaker
    db_connection._async_engine = engine
    db_connection._async_sessionmaker = session_factory

    
    async with session_factory() as session:
        # Clean transient test data between tests to ensure test isolation
        from sqlalchemy import text
        await session.execute(text("DELETE FROM player_stats"))
        await session.execute(text("DELETE FROM matchmaking_queue"))
        await session.execute(text("DELETE FROM match_players"))
        await session.execute(text("DELETE FROM matches"))
        await session.execute(text("DELETE FROM party_members"))
        await session.execute(text("DELETE FROM parties"))
        await session.execute(text("DELETE FROM ratings"))
        await session.execute(text("DELETE FROM groups"))
        await session.execute(text("DELETE FROM ad_campaigns"))
        await session.execute(text("DELETE FROM sponsor_channels"))
        await session.execute(text("DELETE FROM games"))
        await seed_default_data(session, include_sample_ad=False)
        await session.commit()

        yield session
        await session.rollback()
        
    db_connection._async_engine = previous_engine
    db_connection._async_sessionmaker = previous_sessionmaker
    await engine.dispose()
