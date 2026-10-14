"""Announcement pinning: a new invasion must be impossible to miss.

The announcement is pinned (with a notification) as soon as it is delivered,
and the group's previous pinned announcement is unpinned at that moment — not
when the earlier battle ended. SQLite-only DB and mocked Telegram; no bot
startup and no live group.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import PinChatMessage, UnpinChatMessage
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models import Inv2Invasion, Inv2Reward
from sib.services.invasion_v2 import presentation
from sib.tests.invasion_v2.test_presentation_phase5 import NOW, create_inv, db

MIGRATION = (Path(__file__).resolve().parents[2]
             / "database/migrations/versions/042_inv2_pinned_announcement.py")


def _bot(*, message_id=123, **overrides):
    """A bot that accepts everything, unless a call is overridden."""
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-100), message_id=message_id)),
        edit_message_caption=AsyncMock(return_value=True),
        pin_chat_message=AsyncMock(return_value=True),
        unpin_chat_message=AsyncMock(return_value=True))
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


async def _announce(factory, bot, **invasion_kwargs):
    inv_id = await create_inv(factory, **invasion_kwargs)
    await presentation.InvasionPresentation().announce(bot, inv_id)
    return inv_id


@pytest.mark.asyncio
async def test_announcement_is_pinned_with_notification(db):
    factory, _ = db
    bot = _bot()
    inv_id = await _announce(factory, bot)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=-100, message_id=123, disable_notification=False)
    bot.unpin_chat_message.assert_not_awaited()  # nothing of ours was pinned yet
    async with factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert inv.status == "ACTIVE" and inv.message_id == 123
        assert inv.pinned_at is not None


@pytest.mark.asyncio
async def test_next_invasion_unpins_previous_and_pins_new(db):
    factory, _ = db
    first_bot = _bot(message_id=123)
    first = await _announce(factory, first_bot)
    async with factory.begin() as session:  # the first battle ends; pin remains
        (await session.get(Inv2Invasion, first)).status = "VICTORY"
    second_bot = _bot(message_id=456)
    second = await _announce(factory, second_bot, hp=201)
    second_bot.pin_chat_message.assert_awaited_once_with(
        chat_id=-100, message_id=456, disable_notification=False)
    second_bot.unpin_chat_message.assert_awaited_once_with(chat_id=-100, message_id=123)
    async with factory() as session:
        assert (await session.get(Inv2Invasion, first)).pinned_at is None
        assert (await session.get(Inv2Invasion, second)).pinned_at is not None


@pytest.mark.asyncio
async def test_other_groups_pins_are_left_alone(db):
    factory, _ = db
    from sib.database.models import Group

    async with factory.begin() as session:
        session.add(Group(id=-200, title="گروه دوم"))
    mine = await _announce(factory, _bot(message_id=123))
    other_bot = _bot(message_id=777)
    await _announce(factory, other_bot, group=-200, hp=202)
    other_bot.unpin_chat_message.assert_not_awaited()
    async with factory() as session:
        assert (await session.get(Inv2Invasion, mine)).pinned_at is not None


@pytest.mark.asyncio
async def test_pin_rejection_keeps_the_invasion_live_and_the_breaker_clean(db):
    factory, _ = db
    bot = _bot(pin_chat_message=AsyncMock(side_effect=TelegramBadRequest(
        method=PinChatMessage(chat_id=-100, message_id=123),
        message="not enough rights to manage pinned messages")))
    inv_id = await _announce(factory, bot)
    async with factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert inv.status == "ACTIVE" and inv.message_id == 123  # delivery survived
        assert inv.pinned_at is None  # only accepted pins are recorded
    bot.unpin_chat_message.assert_not_awaited()  # old pin kept over nothing pinned
    # A missing pin right must never mute the group: only the photo is metered.
    presentation.record_send_failure.assert_not_awaited()
    presentation.record_send_success.assert_awaited_once_with(-100)


@pytest.mark.asyncio
async def test_bot_without_pin_support_still_announces(db):
    factory, _ = db
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123)))
    inv_id = await _announce(factory, bot)
    async with factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert inv.status == "ACTIVE" and inv.pinned_at is None
    presentation.record_send_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_unpin_leaves_the_previous_row_marked_for_retry(db):
    factory, _ = db
    first = await _announce(factory, _bot(message_id=123))
    async with factory.begin() as session:
        (await session.get(Inv2Invasion, first)).status = "DEFEAT"
    second_bot = _bot(message_id=456, unpin_chat_message=AsyncMock(
        side_effect=TelegramBadRequest(method=UnpinChatMessage(chat_id=-100),
                                       message="message to unpin not found")))
    second = await _announce(factory, second_bot, hp=201)
    second_bot.pin_chat_message.assert_awaited_once()
    async with factory() as session:
        assert (await session.get(Inv2Invasion, first)).pinned_at is not None
        assert (await session.get(Inv2Invasion, second)).pinned_at is not None


@pytest.mark.asyncio
async def test_duplicate_announce_does_not_pin_twice(db):
    factory, _ = db
    bot = _bot()
    inv_id = await _announce(factory, bot)
    await presentation.InvasionPresentation().announce(bot, inv_id)
    bot.send_photo.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_keeps_the_announcement_pinned(db):
    """Agreed behaviour: the pin survives the result and only the next
    invasion removes it."""
    factory, _ = db
    bot = _bot()
    inv_id = await _announce(factory, bot)
    async with factory.begin() as session:
        (await session.get(Inv2Invasion, inv_id)).status = "VICTORY"
        session.add(Inv2Reward(invasion_id=inv_id, user_id=1,
                               reward_type="golden_apple", reward_amount=3, granted_at=NOW))
    await presentation.InvasionPresentation().finalize(bot, inv_id)
    bot.unpin_chat_message.assert_not_awaited()
    async with factory() as session:
        assert (await session.get(Inv2Invasion, inv_id)).pinned_at is not None


def test_migration_042_adds_and_drops_pinned_at(tmp_path):
    """The column is additive: existing announcement rows survive both ways."""
    spec = importlib.util.spec_from_file_location("migration_042", MIGRATION)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "041_inv2_announcement_narrative"
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'pin.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE inv2_invasions (id INTEGER PRIMARY KEY, message_id BIGINT)"))
        connection.execute(sa.text("INSERT INTO inv2_invasions (id, message_id) VALUES (7, 123)"))
    for direction, present in (("upgrade", True), ("downgrade", False)):
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, direction)()
        columns = {column["name"] for column in sa.inspect(engine).get_columns("inv2_invasions")}
        assert ("pinned_at" in columns) is present
        with engine.begin() as connection:
            assert connection.execute(
                sa.text("SELECT message_id FROM inv2_invasions WHERE id = 7")).scalar() == 123
    engine.dispose()
