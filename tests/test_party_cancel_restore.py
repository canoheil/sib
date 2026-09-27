"""Phase 5 regressions: party cancellation restores the menu (solo/2v2/4v4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.types import Chat as TelegramChat
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from sib.database.models import Game, PartyStatus
from sib.handlers.party import handlers as party_handlers
from sib.handlers.party.handlers import handle_cancel_party, handle_leave_party
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(party_handlers, "PARTY_CANCEL_RESTORE_DELAY", 0)


def _cb(user, group, data):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=group.id, type="supergroup", title=group.title)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


async def _game_with_mode(db_session, mode: str):
    flag = {"solo": "supports_solo", "2v2": "supports_2v2", "4v4": "supports_4v4"}[mode]
    game = await db_session.scalar(
        select(Game).where(Game.is_active == True).where(getattr(Game, flag) == True)
    )
    assert game is not None, f"no seeded game supports {mode}"
    return game


def _assert_cancel_state_then_menu(cb, cancel_wording: str):
    """The message shows the established cancel wording first and ends as
    the games menu - never a dead message."""
    assert cb.message.edit_text.await_count == 2
    first_text = cb.message.edit_text.await_args_list[0].args[0]
    first_kb = cb.message.edit_text.await_args_list[0].kwargs.get("reply_markup")
    assert cancel_wording in first_text
    assert first_kb is None  # the transient cancellation state

    second_text = cb.message.edit_text.await_args_list[1].args[0]
    second_kb = cb.message.edit_text.await_args_list[1].kwargs.get("reply_markup")
    assert "منوی بازی‌های سیب" in second_text  # restored menu
    assert second_kb is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["solo", "2v2", "4v4"])
async def test_cancel_restores_menu_for_all_party_modes(db_session, mode):
    group = await create_test_group(db_session, f"گروه لغو {mode}")
    user = await create_test_user(db_session, f"سازنده {mode}")
    game = await _game_with_mode(db_session, mode)

    party = await PartyService.create_party(
        db_session, group.id, user.id, game.code, mode
    )
    await db_session.commit()

    cb = _cb(user, group, f"cancel_party:{party.id}")
    await handle_cancel_party(cb)

    _assert_cancel_state_then_menu(cb, "این پارتی توسط کاربر لغو گردید")

    # Cancellation itself is persisted (cleanup happens in the service).
    await db_session.rollback()
    from sib.database.models import Party

    p = await db_session.scalar(
        select(Party).where(Party.id == party.id).execution_options(populate_existing=True)
    )
    assert p.status == PartyStatus.CANCELLED


@pytest.mark.asyncio
async def test_leave_cascade_also_restores_menu(db_session):
    """Last member leaving cancels the party - wording kept, menu restored."""
    group = await create_test_group(db_session, "گروه خروج")
    user = await create_test_user(db_session, "تنها عضو")
    game = await _game_with_mode(db_session, "solo")

    party = await PartyService.create_party(
        db_session, group.id, user.id, game.code, "solo"
    )
    await db_session.commit()

    cb = _cb(user, group, f"leave_party:{party.id}")
    await handle_leave_party(cb)

    _assert_cancel_state_then_menu(cb, "این پارتی لغو شد")


@pytest.mark.asyncio
async def test_non_member_cannot_cancel_and_message_untouched(db_session):
    """Authorization intact: outsiders cannot cancel and no menu is shown."""
    group = await create_test_group(db_session, "گروه امنیت")
    creator = await create_test_user(db_session, "سازنده")
    outsider = await create_test_user(db_session, "بیگانه")
    game = await _game_with_mode(db_session, "solo")

    party = await PartyService.create_party(
        db_session, group.id, creator.id, game.code, "solo"
    )
    await db_session.commit()

    cb = _cb(outsider, group, f"cancel_party:{party.id}")
    await handle_cancel_party(cb)

    cb.answer.assert_awaited()
    assert cb.message.edit_text.await_count == 0  # message untouched

    from sib.database.models import Party

    p = await db_session.scalar(
        select(Party).where(Party.id == party.id).execution_options(populate_existing=True)
    )
    assert p.status != PartyStatus.CANCELLED


@pytest.mark.asyncio
async def test_public_participation_still_works_after_changes(db_session):
    """Spec regression guard: joining a 2v2 party stays public for others."""
    group = await create_test_group(db_session, "گروه عمومی")
    u1 = await create_test_user(db_session, "سازنده")
    u2 = await create_test_user(db_session, "مهمان")
    game = await _game_with_mode(db_session, "2v2")

    party = await PartyService.create_party(
        db_session, group.id, u1.id, game.code, "2v2"
    )
    await db_session.commit()

    cb = _cb(u2, group, f"join_party:{party.id}")
    from sib.handlers.party.handlers import handle_join_party

    await handle_join_party(cb)
    assert "اضافه شدی" in (cb.answer.await_args.args[0] if cb.answer.await_args else "")
