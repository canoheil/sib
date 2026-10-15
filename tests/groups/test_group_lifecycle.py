"""Isolated tests for bot-driven group registration and deactivation."""

import importlib.util
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import AdminLog, Base, Group
from sib.handlers import group_lifecycle as lifecycle
from sib.handlers import group_lifecycle_router, register_all_handlers


@pytest_asyncio.fixture
async def lifecycle_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[Group.__table__, AdminLog.__table__],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def session_scope():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(lifecycle, "get_session", session_scope)
    yield factory
    await engine.dispose()


def _member_event(
    *,
    old_status="left",
    new_status="member",
    chat_type="group",
    group_id=-100123,
    title="گروه آزمایشی",
    actor_id=7001,
    is_bot=True,
):
    bot_user = SimpleNamespace(id=9001, is_bot=is_bot)
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=group_id,
            type=chat_type,
            title=title,
            username="test_group",
        ),
        from_user=SimpleNamespace(id=actor_id),
        old_chat_member=SimpleNamespace(status=old_status, user=bot_user),
        new_chat_member=SimpleNamespace(status=new_status, user=bot_user),
    )


async def _seed_group(factory, *, group_id=-100123, active=True, welcomed_at=None):
    async with factory.begin() as session:
        session.add(Group(
            id=group_id,
            title="گروه قبلی",
            is_active=active,
            welcomed_at=welcomed_at,
        ))


@pytest.mark.asyncio
async def test_bot_added_creates_group_with_is_active_true(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(_member_event(), bot)

    async with lifecycle_db() as session:
        group = await session.get(Group, -100123)
        assert group is not None
        assert group.title == "گروه آزمایشی"
        assert group.is_active is True
        assert group.welcomed_at is not None
    bot.send_message.assert_awaited_once_with(
        chat_id=-100123,
        text="🍏 سلام! من سیب‌ام. چند پیام در گروه رد و بدل کنید تا آماده شوم.",
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_bot_added_twice_does_not_duplicate_or_reset_welcomed_at(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))
    await lifecycle.handle_my_chat_member(_member_event(), bot)

    original_welcomed_at = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with lifecycle_db.begin() as session:
        group = await session.get(Group, -100123)
        group.welcomed_at = original_welcomed_at

    # Telegram may deliver a second join/promote transition for this group.
    await lifecycle.handle_my_chat_member(
        _member_event(
            old_status="restricted",
            new_status="administrator",
            title="نام تازه گروه",
        ),
        bot,
    )

    async with lifecycle_db() as session:
        groups = list((await session.scalars(select(Group))).all())
        assert len(groups) == 1
        assert groups[0].title == "نام تازه گروه"
        assert groups[0].welcomed_at.replace(tzinfo=timezone.utc) == original_welcomed_at
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_welcome_sent_exactly_once(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(_member_event(), bot)
    await lifecycle.handle_my_chat_member(
        _member_event(old_status="restricted", new_status="administrator"),
        bot,
    )

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_promoted_does_not_reactivate_if_already_active(lifecycle_db):
    welcomed_at = datetime(2021, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    await _seed_group(lifecycle_db, active=True, welcomed_at=welcomed_at)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(
        _member_event(old_status="restricted", new_status="administrator"),
        bot,
    )

    async with lifecycle_db() as session:
        group = await session.get(Group, -100123)
        assert group.is_active is True
        assert group.welcomed_at.replace(tzinfo=timezone.utc) == welcomed_at
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_removed_sets_is_active_false(lifecycle_db):
    await _seed_group(lifecycle_db, active=True)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(
        _member_event(old_status="administrator", new_status="left"),
        bot,
    )

    async with lifecycle_db() as session:
        group = await session.get(Group, -100123)
        assert group.is_active is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_chat_ignored(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(_member_event(chat_type="private"), bot)

    async with lifecycle_db() as session:
        assert await session.scalar(select(func.count()).select_from(Group)) == 0
        assert await session.scalar(select(func.count()).select_from(AdminLog)) == 0
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_supergroup_handled_same_as_group(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(
        _member_event(chat_type="supergroup", group_id=-100456),
        bot,
    )

    async with lifecycle_db() as session:
        group = await session.get(Group, -100456)
        assert group is not None
        assert group.is_active is True
        assert group.welcomed_at is not None
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_logs_written_for_register_and_deactivate(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))
    await lifecycle.handle_my_chat_member(_member_event(actor_id=7001), bot)
    await lifecycle.handle_my_chat_member(
        _member_event(
            old_status="administrator",
            new_status="kicked",
            actor_id=7002,
        ),
        bot,
    )

    async with lifecycle_db() as session:
        logs = list((await session.scalars(select(AdminLog).order_by(AdminLog.id))).all())
        assert [log.action for log in logs] == [
            "group_auto_registered",
            "group_deactivated",
        ]
        assert logs[0].admin_id == 7001
        assert json.loads(logs[0].details) == {
            "group_id": -100123,
            "title": "گروه آزمایشی",
        }
        assert logs[1].admin_id == 7002
        assert json.loads(logs[1].details) == {"group_id": -100123}


@pytest.mark.asyncio
async def test_exception_in_handler_does_not_crash_dispatcher(monkeypatch):
    @asynccontextmanager
    async def broken_session():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover - makes this an async context manager

    monkeypatch.setattr(lifecycle, "get_session", broken_session)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    # The handler is the aiogram observer callback; storage errors are logged
    # and swallowed so polling can continue processing later updates.
    await lifecycle.handle_my_chat_member(_member_event(), bot)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_welcome_send_failure_does_not_undo_registration(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("forbidden")))

    await lifecycle.handle_my_chat_member(_member_event(), bot)

    async with lifecycle_db() as session:
        group = await session.get(Group, -100123)
        assert group is not None
        assert group.is_active is True
        assert group.welcomed_at is not None
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unlisted_status_transition_is_ignored(lifecycle_db):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=True))

    await lifecycle.handle_my_chat_member(
        _member_event(old_status="member", new_status="restricted"),
        bot,
    )

    async with lifecycle_db() as session:
        assert await session.scalar(select(func.count()).select_from(Group)) == 0
        assert await session.scalar(select(func.count()).select_from(AdminLog)) == 0
    bot.send_message.assert_not_awaited()


def test_group_lifecycle_router_is_registered_with_dispatcher():
    class CaptureDispatcher:
        def __init__(self):
            self.routers = []

        def include_router(self, router):
            self.routers.append(router)

    dispatcher = CaptureDispatcher()
    register_all_handlers(dispatcher)

    assert group_lifecycle_router in dispatcher.routers
    assert group_lifecycle_router.name == "group_lifecycle"


def test_045_group_welcomed_at_migration_is_reversible():
    migration_path = Path(__file__).resolve().parents[2] / "database/migrations/versions/045_group_welcomed_at.py"
    spec = importlib.util.spec_from_file_location("migration_045_group_welcomed_at", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE groups (id INTEGER PRIMARY KEY)")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("groups")}
        assert "welcomed_at" in columns
        assert columns["welcomed_at"]["nullable"] is True
        assert str(columns["welcomed_at"]["type"]).upper() == "DATETIME"

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "welcomed_at" not in {
            column["name"] for column in inspect(connection).get_columns("groups")
        }
    engine.dispose()
