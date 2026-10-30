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


def _using_sqlite() -> bool:
    """True when the test database is SQLite (no real row-level locks)."""
    return "sqlite" in (os.getenv("sib_TEST_DATABASE_URL") or "")


# ``SELECT ... FOR UPDATE`` row locking is a MySQL/MariaDB feature that
# SQLite silently ignores - concurrency race tests need a row-lock engine
# to be meaningful. On MySQL/MariaDB these tests always run.
requires_row_locks = pytest.mark.skipif(
    _using_sqlite(),
    reason="SELECT ... FOR UPDATE row locking requires MySQL/MariaDB; "
    "SQLite cannot exercise this race",
)

@pytest.fixture(autouse=True)
def golden_hour_off_by_default(request, monkeypatch):
    """Keep legacy reward assertions independent of the wall clock.

    Tests that intentionally cover the golden-hour feature opt in with the
    ``golden_hour`` marker and pass an explicit event timestamp.
    """
    if request.node.get_closest_marker("golden_hour") is not None:
        return

    import sib.services.golden_hour as golden_hour

    monkeypatch.setattr(golden_hour, "is_golden_hour", lambda now=None: False)


@pytest.fixture(scope="session")
def test_settings():
    """Fixture providing settings configured for testing."""
    return settings


def _create_all_compatible(connection) -> None:
    """``Base.metadata.create_all`` with a SQLite-only compatibility shim.

    Production runs on MySQL/MariaDB where a BIGINT primary key is created
    with AUTO_INCREMENT automatically. SQLite only autoincrements plain
    INTEGER primary keys, so wallet/ledger-style inserts would fail there.
    When the test database is SQLite we narrow those PK column types to
    INTEGER *for DDL purposes only*; MySQL/MariaDB runs are untouched.
    """
    from sqlalchemy import Integer
    from sqlalchemy.dialects.sqlite.base import SQLiteDialect

    if isinstance(connection.dialect, SQLiteDialect):
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if (
                    col.primary_key
                    and type(col.type).__name__ == "BigInteger"
                ):
                    col.type = Integer()
    Base.metadata.create_all(connection)


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
        await conn.run_sync(_create_all_compatible)

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
        await session.execute(text("DELETE FROM halloween_loot_drop_claims"))
        await session.execute(text("DELETE FROM halloween_loot_drops"))
        await session.execute(text("DELETE FROM halloween_seed_cooldowns"))
        await session.execute(text("DELETE FROM halloween_reward_claims"))
        await session.execute(text("DELETE FROM halloween_attacks"))
        await session.execute(text("DELETE FROM halloween_event_state"))
        await session.execute(text("DELETE FROM castle_income_claims"))
        await session.execute(text("DELETE FROM castle_donations"))
        await session.execute(text("DELETE FROM war_attacks"))
        await session.execute(text("DELETE FROM war_participants"))
        await session.execute(text("DELETE FROM group_wars"))
        await session.execute(text("DELETE FROM group_castles"))
        await session.execute(text("DELETE FROM group_league_awards"))
        await session.execute(text("DELETE FROM group_league_weeks"))
        await session.execute(text("DELETE FROM player_xp_grants"))
        await session.execute(text("DELETE FROM player_progressions"))
        await session.execute(text("DELETE FROM flower_gifts"))
        await session.execute(text("DELETE FROM apple_friendships"))
        await session.execute(text("DELETE FROM marriages"))
        await session.execute(text("DELETE FROM marriage_proposals"))
        await session.execute(text("DELETE FROM apple_duel_slots"))
        await session.execute(text("DELETE FROM apple_duels"))
        await session.execute(text("DELETE FROM apple_mine_shift"))
        await session.execute(text("DELETE FROM apple_battle_queue"))
        await session.execute(text("DELETE FROM apple_battles"))
        await session.execute(text("DELETE FROM user_items"))
        await session.execute(text("DELETE FROM user_wanted_states"))
        await session.execute(text("DELETE FROM user_diseases"))
        await session.execute(text("DELETE FROM daily_chest_claims"))
        await session.execute(text("DELETE FROM daily_channel_posts"))
        await session.execute(text("DELETE FROM golden_hour_group_announcements"))
        await session.execute(text("DELETE FROM bot_assets"))
        await session.execute(text("DELETE FROM trades"))
        await session.execute(text("DELETE FROM shop_entries"))
        await session.execute(text("DELETE FROM shop_rotations"))
        await session.execute(text("DELETE FROM shop_chest_openings"))
        await session.execute(text("DELETE FROM shop_chest_rewards"))
        await session.execute(text("DELETE FROM shop_chests"))
        await session.execute(text("DELETE FROM player_cards"))
        await session.execute(text("DELETE FROM character_balance_versions"))
        await session.execute(text("DELETE FROM characters"))
        await session.execute(text("DELETE FROM factions"))
        await session.execute(text("DELETE FROM apple_transactions"))
        await session.execute(text("DELETE FROM apple_wallets"))
        await session.execute(text("DELETE FROM garden_wither_events"))
        await session.execute(text("DELETE FROM garden_plots"))
        await session.execute(text("DELETE FROM gardens"))
        await session.execute(text("DELETE FROM inv2_attacks"))
        await session.execute(text("DELETE FROM inv2_rewards"))
        await session.execute(text("DELETE FROM inv2_invasions"))
        await session.execute(text("DELETE FROM inv2_group_members"))
        await session.execute(text("DELETE FROM inv2_group_state"))
        await session.execute(text("DELETE FROM inv2_regions"))
        await session.execute(text("DELETE FROM system_config"))
        await session.execute(text("DELETE FROM seedling_awards"))
        await session.execute(text("DELETE FROM seedling_inventories"))
        await session.execute(text("DELETE FROM player_stats"))
        await session.execute(text("DELETE FROM matchmaking_queue"))
        await session.execute(text("DELETE FROM match_players"))
        await session.execute(text("DELETE FROM matches"))
        await session.execute(text("DELETE FROM party_members"))
        await session.execute(text("DELETE FROM parties"))
        await session.execute(text("DELETE FROM group_stats"))
        await session.execute(text("DELETE FROM panel_owners"))
        await session.execute(text("DELETE FROM groups"))
        await session.execute(text("DELETE FROM ad_campaigns"))
        await session.execute(text("DELETE FROM sponsor_channels"))
        await session.execute(text("DELETE FROM games"))
        await seed_default_data(session, include_sample_ad=False)
        await session.commit()
        from sib.services.halloween import invalidate_halloween_config_cache

        invalidate_halloween_config_cache()

        yield session
        await session.rollback()

    # Bot turns are detached background tasks by design: cancel the ones the
    # test left sleeping so no test can leak a task into the next one (and
    # the task never outlives the test-scoped database engine).
    try:
        from sib.bots.runner import cancel_all_bot_turns
        from sib.services.apple_battle import AppleBattleService

        await cancel_all_bot_turns()
        await AppleBattleService.cancel_all_narrations()
    except Exception:  # pragma: no cover - cleanup is best effort
        pass

    db_connection._async_engine = previous_engine
    db_connection._async_sessionmaker = previous_sessionmaker
    await engine.dispose()
