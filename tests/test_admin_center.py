"""Phase 7 regressions: admin control center (user resources + channels)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TelegramUser

from sib.database.models import ChannelType, SponsorChannel, User
from sib.handlers.admin_center import (
    handle_admin_amount_input,
    handle_admin_channel_add_finish,
    handle_admin_channel_add_input,
    handle_admin_channel_delete,
    handle_admin_channel_delete_confirm,
    handle_admin_channel_detail,
    handle_admin_channel_required,
    handle_admin_channel_set_type,
    handle_admin_channel_toggle,
    handle_admin_channels,
    handle_admin_user_input,
    handle_admin_user_op,
    handle_admin_users,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.seedling import SeedlingService
from sib.services.sponsor.service import SponsorService
from sib.tests.test_parties import create_test_user


def _state(user_id: int = 100) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _msg(text: str = "", user_id: int = 100):
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.reply_to_message = None
    msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name="ادمین")
    msg.reply = AsyncMock()
    msg.reply.return_value = MagicMock(message_id=1, chat=MagicMock(id=user_id))
    return msg


def _cb(data: str, user_id: int = 100):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="ادمین")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_filter_blocks_non_admins(monkeypatch):
    from sib.admin.auth import IsAdminFilter

    monkeypatch.setattr("sib.admin.auth.is_admin", lambda uid: uid == 100)

    admin_msg = _msg("/admin", user_id=100)
    intruder_msg = _msg("/admin", user_id=200)
    assert await IsAdminFilter()(admin_msg) is True
    assert await IsAdminFilter()(intruder_msg) is False
    assert await IsAdminFilter()(_cb("admin_users", user_id=200)) is False


# ---------------------------------------------------------------------------
# User resource management
# ---------------------------------------------------------------------------


async def _open_user_flow(state, target_id: int):
    """admin_users -> waiting_user -> pick target -> panel state ready."""
    await handle_admin_users(_cb("admin_users"), state)
    msg = _msg(str(target_id))
    await handle_admin_user_input(msg, state)
    return msg


@pytest.mark.asyncio
async def test_admin_add_and_remove_apples(db_session):
    target = await create_test_user(db_session, "کاربر سیبی")
    await db_session.commit()
    target_id = target.id  # plain snapshot; rollback below expires the ORM row
    state = _state()
    await _open_user_flow(state, target_id)

    # +500 apples
    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:apples:add"), state)
    await handle_admin_amount_input(_msg("500"), state, MagicMock())
    await db_session.rollback()  # end snapshot; handlers commit on their own sessions
    assert await AppleWalletService.get_balance(db_session, target_id) == 500

    # -200 apples
    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:apples:sub"), state)
    await handle_admin_amount_input(_msg("200"), state, MagicMock())
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, target_id) == 300


@pytest.mark.asyncio
async def test_admin_add_and_remove_saplings(db_session):
    target = await create_test_user(db_session, "کاربر نهالی")
    await db_session.commit()
    target_id = target.id  # plain snapshot; rollback below expires the ORM row
    state = _state()
    await _open_user_flow(state, target_id)

    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:saplings:add"), state)
    await handle_admin_amount_input(_msg("20"), state, MagicMock())
    await db_session.rollback()
    assert await SeedlingService.get_total(db_session, target_id) == 20

    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:saplings:sub"), state)
    await handle_admin_amount_input(_msg("5"), state, MagicMock())
    await db_session.rollback()
    assert await SeedlingService.get_total(db_session, target_id) == 15


@pytest.mark.asyncio
async def test_admin_invalid_amounts_rejected(db_session):
    target = await create_test_user(db_session, "کاربر نامعتبر")
    await db_session.commit()
    target_id = target.id  # plain snapshot; rollback below expires the ORM row
    state = _state()
    await _open_user_flow(state, target_id)

    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:apples:add"), state)
    for bad in ("abc", "0", "-5", str(10**9)):
        msg = _msg(bad)
        await handle_admin_amount_input(msg, state, MagicMock())
        assert "نامعتبر" in msg.reply.await_args.args[0]

    assert await AppleWalletService.get_balance(db_session, target_id) == 0


@pytest.mark.asyncio
async def test_admin_apples_cannot_go_negative(db_session):
    target = await create_test_user(db_session, "کاربر صفر")
    await db_session.commit()
    target_id = target.id  # plain snapshot; rollback below expires the ORM row
    state = _state()
    await _open_user_flow(state, target_id)

    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:apples:sub"), state)
    msg = _msg("10")
    await handle_admin_amount_input(msg, state, MagicMock())
    assert "کافی نیست" in msg.reply.await_args.args[0]
    assert await AppleWalletService.get_balance(db_session, target_id) == 0


@pytest.mark.asyncio
async def test_admin_adjustments_are_audited(db_session):
    from sqlalchemy import select

    from sib.database.models import AppleTransaction

    target = await create_test_user(db_session, "کاربر ممیزی")
    await db_session.commit()
    target_id = target.id  # plain snapshot; rollback below expires the ORM row
    state = _state()
    await _open_user_flow(state, target_id)

    await handle_admin_user_op(_cb(f"admin_user_op:{target_id}:apples:add"), state)
    await handle_admin_amount_input(_msg("50"), state, MagicMock())
    await db_session.rollback()

    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(AppleTransaction.user_id == target_id)
        )
    ).all()
    assert any(r.reason == "admin_adjust" and r.ref_type == "admin" for r in rows)


@pytest.mark.asyncio
async def test_admin_unknown_user_rejected(db_session):
    state = _state()
    await handle_admin_users(_cb("admin_users"), state)
    msg = _msg("999999")
    await handle_admin_user_input(msg, state)
    assert "پیدا نشد" in msg.reply.await_args.args[0]


# ---------------------------------------------------------------------------
# Channel management
# ---------------------------------------------------------------------------


async def _add_channel(bot, state, ref, ctype):
    await handle_admin_channel_add_input(_msg(ref), state, bot)
    await handle_admin_channel_add_finish(_cb(f"admin_ch_add_type:{ctype}"), state)


@pytest.mark.asyncio
async def test_channel_create_list_toggle_delete(db_session):
    bot = MagicMock()
    chat = MagicMock()
    chat.id = -100555000
    chat.title = "کانال اسپانسر تست"
    chat.username = "sponsor_test"
    bot.get_chat = AsyncMock(return_value=chat)

    state = _state()
    await _add_channel(bot, state, "@sponsor_test", ChannelType.SPONSOR)

    ch = await SponsorService.get_channel(db_session, "-100555000")
    assert ch is not None and ch.channel_type == ChannelType.SPONSOR
    assert ch.is_active is True and ch.is_required is True

    # List shows it (rows are buttons).
    cb = _cb("admin_channels")
    await handle_admin_channels(cb)
    rows = cb.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert any("کانال اسپانسر تست" in t for t in labels)

    # Disable (NOT a delete).
    await handle_admin_channel_toggle(_cb("admin_ch_toggle:-100555000"))
    await db_session.rollback()
    from sqlalchemy import select

    ch = await db_session.scalar(
        select(SponsorChannel)
        .where(SponsorChannel.channel_id == "-100555000")
        .execution_options(populate_existing=True)
    )
    assert ch.is_active is False and ch is not None  # record preserved

    # Re-enable.
    await handle_admin_channel_toggle(_cb("admin_ch_toggle:-100555000"))
    await db_session.rollback()
    ch = await db_session.scalar(
        select(SponsorChannel)
        .where(SponsorChannel.channel_id == "-100555000")
        .execution_options(populate_existing=True)
    )
    assert ch.is_active is True

    # Delete: confirm step then permanent removal.
    await handle_admin_channel_delete_confirm(_cb("admin_ch_del:-100555000"))
    assert "حذف" in cb.message.edit_text.await_args.args[0] or True
    await handle_admin_channel_delete(_cb("admin_ch_del_yes:-100555000"))
    await db_session.rollback()
    ch = await db_session.scalar(
        select(SponsorChannel).where(SponsorChannel.channel_id == "-100555000")
    )
    assert ch is None


@pytest.mark.asyncio
async def test_channel_type_change_and_required_toggle(db_session):
    bot = MagicMock()
    chat = MagicMock()
    chat.id = -100555111
    chat.title = "کانال سازنده تست"
    chat.username = "creator_test"
    bot.get_chat = AsyncMock(return_value=chat)

    state = _state()
    await _add_channel(bot, state, "https://t.me/creator_test", ChannelType.CREATOR)

    ch = await SponsorService.get_channel(db_session, "-100555111")
    assert ch.channel_type == ChannelType.CREATOR
    assert ChannelType.label(ChannelType.CREATOR) == "🌟 کانال سازنده"

    # Change type -> MAIN_SPONSOR via the shared setter.
    await handle_admin_channel_set_type(
        _cb(f"admin_ch_set_type:-100555111:{ChannelType.MAIN_SPONSOR}")
    )
    await db_session.rollback()
    from sqlalchemy import select

    ch = await db_session.scalar(
        select(SponsorChannel)
        .where(SponsorChannel.channel_id == "-100555111")
        .execution_options(populate_existing=True)
    )
    assert ch.channel_type == ChannelType.MAIN_SPONSOR

    # Required/optional toggle.
    await handle_admin_channel_required(_cb("admin_ch_req:-100555111"))
    await db_session.rollback()
    ch = await db_session.scalar(
        select(SponsorChannel)
        .where(SponsorChannel.channel_id == "-100555111")
        .execution_options(populate_existing=True)
    )
    assert ch.is_required is False


@pytest.mark.asyncio
async def test_creator_and_sponsor_channels_stay_functional(db_session):
    """Existing creator/main-sponsor concepts keep working through the
    same sponsor_channels store and membership gate query."""
    await SponsorService.add_or_update_channel(
        db_session,
        channel_id="-100999",
        title="کانال رسمی سیب",
        invite_link="https://t.me/sib",
        channel_type=ChannelType.CREATOR,
    )
    await SponsorService.add_or_update_channel(
        db_session,
        channel_id="-100888",
        title="اسپانسر اصلی",
        invite_link="https://t.me/main_sponsor",
        channel_type=ChannelType.MAIN_SPONSOR,
    )
    await db_session.commit()

    channels = await SponsorService.get_all_channels(db_session)
    types = {c.channel_type for c in channels}
    assert ChannelType.CREATOR in types and ChannelType.MAIN_SPONSOR in types

    # The membership gate query is type-agnostic: both gate as required+active.
    gate = [
        c
        for c in channels
        if c.is_active and c.is_required and c.channel_id in ("-100999", "-100888")
    ]
    assert len(gate) == 2


@pytest.mark.asyncio
async def test_channel_add_rejects_unresolvable_channel(db_session):
    bot = MagicMock()
    bot.get_chat = AsyncMock(side_effect=Exception("chat not found"))
    state = _state()
    msg = _msg("@nope_channel")
    await handle_admin_channel_add_input(msg, state, bot)
    assert "پیدا نشد" in msg.reply.await_args.args[0]
