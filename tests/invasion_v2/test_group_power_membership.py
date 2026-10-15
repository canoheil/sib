"""Phase 4.5: actual Main Card power, observation and historical backfill."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import (
    Base, Character, Game, Group, Inv2GroupMember, Match, MatchPlayer,
    PlayerCard, SystemConfig, User,
)
from sib.middleware import group_membership as hook
from sib.scripts.backfill_inv2_group_members import backfill
from sib.services.invasion_v2.power_resolver_impl import DbGroupPowerResolver

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    tables = [User.__table__, Group.__table__, Game.__table__, Match.__table__,
              MatchPlayer.__table__, Character.__table__, PlayerCard.__table__,
              Inv2GroupMember.__table__, SystemConfig.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add_all([User(id=1, first_name="One"), User(id=2, first_name="Two"),
                         User(id=3, first_name="Three"), User(id=9, first_name="Bot", is_bot=True),
                         Group(id=-100, title="Group"), Group(id=-200, title="Other"),
                         Character(id=10, code="test", name_fa="Test", power=20, speed=20, intelligence=20),
                         Character(id=11, code="test-2", name_fa="Test2", power=30, speed=20, intelligence=10)])
    yield factory
    await engine.dispose()


async def observe(sessions, user_id, group_id=-100, seen_at=NOW):
    async with sessions.begin() as session:
        session.add(Inv2GroupMember(user_id=user_id, group_id=group_id, last_seen_at=seen_at))


async def select_main(sessions, user_id, char_id, level):
    async with sessions.begin() as session:
        session.add(PlayerCard(id=user_id, user_id=user_id, character_id=char_id, level=level, is_favorite=True))


async def set_min_players(sessions, value: str) -> None:
    """Override the A6 minimum-players-with-card threshold."""
    async with sessions.begin() as session:
        row = await session.get(SystemConfig, "invasion_min_players_with_card")
        if row is None:
            session.add(SystemConfig(key="invasion_min_players_with_card", value=value))
        else:
            row.value = value


@pytest.mark.asyncio
async def test_no_members_missing_main_and_average_of_levelled_main_cards(sessions):
    resolver = DbGroupPowerResolver()
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) is None
    await observe(sessions, 1)
    async with sessions() as session:
        # A6: zero members with a card is below every threshold, so no average.
        assert await resolver.average_main_card_power(group_id=-100, session=session) is None
    await select_main(sessions, 1, 10, 2)  # displayed power = 20 + 10
    # The shipped threshold is 3; a single card holder only counts once the
    # minimum is lowered to it (the dedicated A6 suite covers the default).
    await set_min_players(sessions, "1")
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) == 30.0
    await observe(sessions, 2)  # A6: a member without a main card is ignored
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) == 30.0
    await select_main(sessions, 2, 11, 3)  # 30 + 20, average 40
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) == 40.0
        assert await resolver.average_main_card_power(group_id=-200, session=session) is None


@pytest.mark.asyncio
async def test_window_and_uncertain_power_fail_closed(sessions):
    await set_min_players(sessions, "1")
    await observe(sessions, 1, seen_at=NOW - timedelta(days=35))
    await observe(sessions, 2, seen_at=NOW - timedelta(days=1))
    await select_main(sessions, 2, 10, 1)
    resolver = DbGroupPowerResolver()
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) == 20.0
    async with sessions.begin() as session:
        session.add(SystemConfig(key="inv2_member_active_window_days", value="60"))
    async with sessions() as session:
        # A6: member 1 is now inside the window but has no card, so it is
        # ignored instead of failing the whole group closed.
        assert await resolver.average_main_card_power(group_id=-100, session=session) == 20.0
    await select_main(sessions, 1, 10, 1)
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) == 20.0
    async with sessions.begin() as session:
        (await session.get(SystemConfig, "inv2_member_active_window_days")).value = "0"
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) is None
    async with sessions.begin() as session:
        (await session.get(SystemConfig, "inv2_member_active_window_days")).value = "30"
        await session.execute(update(PlayerCard).where(PlayerCard.user_id == 2).values(level=99))
    async with sessions() as session:
        # A6 relaxes "no card" only: a card that exists but cannot be scored is
        # corrupt data and still fails closed.
        assert await resolver.average_main_card_power(group_id=-100, session=session) is None
    async with sessions.begin() as session:
        await session.execute(update(PlayerCard).where(PlayerCard.user_id == 2).values(level=1, character_id=999))
    async with sessions() as session:
        assert await resolver.average_main_card_power(group_id=-100, session=session) is None


@pytest.mark.asyncio
async def test_resolver_db_failure_returns_none(sessions, monkeypatch):
    from sib.services.invasion_v2 import power_resolver_impl
    monkeypatch.setattr(power_resolver_impl.ConfigService, "get", AsyncMock(side_effect=RuntimeError("DB down")))
    async with sessions() as session:
        assert await DbGroupPowerResolver().average_main_card_power(group_id=-100, session=session) is None


def telegram_message(user=1, group=-100, chat_type=ChatType.GROUP):
    return Message(message_id=1, date=NOW, chat=Chat(id=group, type=chat_type),
                   from_user=TelegramUser(id=user, is_bot=False, first_name="Tester"))


@pytest.mark.asyncio
async def test_hook_nonblocking_throttled_message_callback_and_upsert(sessions, monkeypatch):
    @asynccontextmanager
    async def db():
        async with sessions.begin() as session:
            yield session
    monkeypatch.setattr(hook, "get_session", db)
    middleware = hook.GroupMembershipMiddleware()
    calls = []

    async def handler(event, data):
        calls.append(event)
        return "original"

    message = telegram_message()
    callback = CallbackQuery(id="cb", from_user=message.from_user, chat_instance="inst", message=message)
    assert await middleware(handler, message, {}) == "original"
    assert await middleware(handler, callback, {}) == "original"  # shared pair throttle
    await asyncio.gather(*list(middleware._tasks))
    async with sessions() as session:
        rows = list((await session.scalars(select(Inv2GroupMember))).all())
        assert len(rows) == 1
        first = rows[0].last_seen_at
        first_updated = rows[0].updated_at
    assert len(calls) == 2
    # Direct concurrent duplicate from an older event must not rewind.
    await hook.record_group_member(1, -100, NOW - timedelta(days=1))
    async with sessions() as session:
        row = await session.scalar(select(Inv2GroupMember))
        assert row.last_seen_at == first
        assert row.updated_at == first_updated  # duplicate changed only last_seen_at
    middleware._recent.clear()
    await middleware(handler, callback, {})
    await asyncio.gather(*list(middleware._tasks))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2GroupMember)) == 1
        assert (await session.scalar(select(Inv2GroupMember.last_seen_at))) >= first


@pytest.mark.asyncio
async def test_hook_never_awaits_write_and_swallows_db_errors(monkeypatch, caplog):
    middleware = hook.GroupMembershipMiddleware()
    gate = asyncio.Event()

    async def stalled(*args):
        await gate.wait()
        raise RuntimeError("DB offline")

    monkeypatch.setattr(hook, "record_group_member", stalled)
    handler = AsyncMock(return_value="ok")
    # The handler completes while the DB task is still suspended.
    result = await asyncio.wait_for(middleware(handler, telegram_message(), {}), 0.1)
    assert result == "ok" and len(middleware._tasks) == 1
    with caplog.at_level(logging.DEBUG, logger="sib"):
        gate.set()
        await asyncio.gather(*list(middleware._tasks))
    assert "inv2 member observation failed" in caplog.text
    # No message from private chats, unknown events or bots is observed.
    assert await middleware(handler, telegram_message(chat_type=ChatType.PRIVATE), {}) == "ok"
    assert hook.GroupMembershipMiddleware._identity(object()) is None
    bot_message = telegram_message().model_copy(update={"from_user": TelegramUser(id=9, is_bot=True, first_name="Bot")})
    assert await middleware(handler, bot_message, {}) == "ok"
    assert not middleware._tasks or all(task.done() for task in middleware._tasks)


@pytest.mark.asyncio
async def test_backfill_dry_run_idempotence_and_newer_observation(sessions):
    old = NOW - timedelta(days=45)
    recent = NOW - timedelta(days=8)
    async with sessions.begin() as session:
        session.add(Game(id=1, code="game", name_fa="Test", supports_solo=True,
                         supports_2v2=False, supports_4v4=False, turn_timeout_seconds=60))
        session.add(Match(id=1, game_id=1))
        session.add_all([
            MatchPlayer(id=1, match_id=1, user_id=1, group_id=-100, team=1,
                        created_at=old, updated_at=old),
            MatchPlayer(id=2, match_id=1, user_id=1, group_id=-100, team=1,
                        created_at=old, updated_at=recent),
            MatchPlayer(id=3, match_id=1, user_id=2, group_id=-200, team=1,
                        created_at=old, updated_at=old),
            MatchPlayer(id=4, match_id=1, user_id=9, group_id=-100, team=1,
                        created_at=old, updated_at=recent),  # simulated bot
            MatchPlayer(id=5, match_id=1, user_id=3, group_id=None, team=1,
                        created_at=old, updated_at=recent),
        ])
    async with sessions() as session:
        preview = await backfill(session, dry_run=True)
        assert (preview.scanned, preview.inserted, preview.advanced) == (2, 2, 0)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2GroupMember)) == 0
    async with sessions.begin() as session:
        run = await backfill(session, dry_run=False)
        assert run == preview
    async with sessions.begin() as session:
        again = await backfill(session, dry_run=False)
        assert (again.scanned, again.inserted, again.unchanged) == (2, 0, 2)
    async with sessions.begin() as session:
        await session.execute(update(MatchPlayer).where(MatchPlayer.id == 1).values(updated_at=NOW))
        advance = await backfill(session, dry_run=False)
        assert (advance.advanced, advance.unchanged) == (1, 1)
    async with sessions() as session:
        rows = list((await session.scalars(select(Inv2GroupMember).order_by(Inv2GroupMember.user_id))).all())
        assert [(row.user_id, row.group_id) for row in rows] == [(1, -100), (2, -200)]
        assert rows[0].last_seen_at >= recent.replace(tzinfo=None)
        assert rows[1].last_seen_at.date() == old.date()
    # A fresh observation must not be replaced with older match history.
    async with sessions.begin() as session:
        rows = list((await session.scalars(select(Inv2GroupMember).where(Inv2GroupMember.user_id == 2))).all())
        rows[0].last_seen_at = NOW
    async with sessions.begin() as session:
        replay = await backfill(session, dry_run=False)
        assert (replay.unchanged, replay.advanced) == (2, 0)


@pytest.mark.asyncio
async def test_backfill_no_source_exits_cleanly():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[User.__table__, Group.__table__, Inv2GroupMember.__table__])
    factory = async_sessionmaker(engine)
    async with factory() as session:
        counts = await backfill(session)
        assert not counts.source_available and counts.scanned == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_cli_dry_run_confirmation_and_no_source(sessions, monkeypatch, capsys):
    import sys
    from sib.scripts import backfill_inv2_group_members as script

    @asynccontextmanager
    async def db():
        async with sessions.begin() as session:
            yield session
    monkeypatch.setattr(script, "get_session", db)
    monkeypatch.setattr(sys, "argv", ["backfill_inv2_group_members", "--dry-run"])
    assert await script._main() == 0  # source exists, but no rows
    assert "dry-run: scanned=0" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["backfill_inv2_group_members"])
    assert await script._main() == 2
    assert "refused" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["backfill_inv2_group_members", "--confirm-database", ":memory:"])
    assert await script._main() == 0
    assert "committed: scanned=0" in capsys.readouterr().out

    # A minimal isolated database without historical match_players is a clean no-op.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[User.__table__, Group.__table__, Inv2GroupMember.__table__])
    factory = async_sessionmaker(engine)

    @asynccontextmanager
    async def minimal_db():
        async with factory.begin() as session:
            yield session
    monkeypatch.setattr(script, "get_session", minimal_db)
    monkeypatch.setattr(sys, "argv", ["backfill_inv2_group_members", "--dry-run"])
    assert await script._main() == 0
    assert "no match_players source table" in capsys.readouterr().out
    await engine.dispose()


def test_mysql_statements_compile_without_mysql_connection():
    from sqlalchemy.dialects.mysql import dialect
    from sib.scripts.backfill_inv2_group_members import _upsert
    seen = NOW
    sql = str(_upsert("mysql", 1, -100, seen).compile(dialect=dialect()))
    assert "ON DUPLICATE KEY UPDATE" in sql and "greatest" in sql.lower()
    with pytest.raises(RuntimeError, match="Unsupported backfill dialect"):
        _upsert("postgresql", 1, -100, seen)


@pytest.mark.asyncio
async def test_hook_mysql_upsert_compiles_and_unsupported_dialect_is_safe(monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy.dialects.mysql import dialect

    fake = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        execute=AsyncMock(),
    )

    @asynccontextmanager
    async def db():
        yield fake

    monkeypatch.setattr(hook, "get_session", db)
    await hook.record_group_member(1, -100, NOW)
    sql = str(fake.execute.await_args.args[0].compile(dialect=dialect()))
    assert "ON DUPLICATE KEY UPDATE" in sql and "greatest" in sql.lower()
    fake.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    with pytest.raises(RuntimeError, match="Unsupported group membership dialect"):
        await hook.record_group_member(1, -100, NOW)
