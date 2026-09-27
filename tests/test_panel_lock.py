"""Spec #5/#6: personal panels are OWNER-LOCKED in the shared layer.

Root-cause test for the cross-user panel bug: User B pressing buttons on
User A's personal panel (profile/garden/cards/shop/trade) must never act -
not even navigation ("main_menu") or refresh. Public game flows must keep
working for everyone.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message
from aiogram.types import User as TelegramUser

from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelOwnershipService,
    is_personal_panel,
)


def _callback(user_id: int, data: str, chat_id: int = -100777, message_id: int = 555):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="کاربر")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type="supergroup", title="گروه")
    cb.message.message_id = message_id
    cb.answer = AsyncMock()
    return cb


async def _run_guard(callback):
    called = []

    async def handler(event, data):
        called.append(event)
        return "HANDLED"

    middleware = PanelGuardMiddleware()
    result = await middleware(handler, callback, {})
    return result, called


def test_personal_panel_classification():
    for t in ("profile", "garden", "cards", "shop", "trade", "card_panel"):
        assert is_personal_panel(t)
    for t in (None, "panel_guard", "party", "wizard"):
        assert not is_personal_panel(t)


@pytest.mark.asyncio
async def test_non_owner_cannot_press_anything_on_personal_panel(db_session):
    # A opens their garden - the message is claimed as a personal panel.
    ok = await PanelOwnershipService.claim(
        db_session, -100777, 555, user_id=111, panel_type="garden"
    )
    await db_session.commit()
    assert ok is True

    # B tries EVERYTHING on A's garden panel - every callback is locked.
    for data in (
        "garden_refresh",
        "garden_harvest:2",
        "garden_plant:3",
        "main_menu",
        "apple_profile",
        "join_party:9",
    ):
        result, called = await _run_guard(_callback(222, data))
        assert result is None, f"B must be locked out of {data}"
        assert called == []
        # The lock message is exactly the spec text.
        assert "🔒" in LOCKED_MESSAGE
        assert "این پنل برای کاربر دیگری باز شده" in LOCKED_MESSAGE


@pytest.mark.asyncio
async def test_owner_operates_own_personal_panel(db_session):
    await PanelOwnershipService.claim(
        db_session, -100777, 556, user_id=111, panel_type="garden"
    )
    await db_session.commit()

    result, called = await _run_guard(_callback(111, "garden_harvest:2"))
    assert result == "HANDLED"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_cross_user_identity_never_leaks(db_session):
    """A shared message must never switch identity to whoever pressed it."""
    await PanelOwnershipService.claim(
        db_session, -100777, 557, user_id=111, panel_type="profile"
    )
    await db_session.commit()

    # B presses a personal callback -> rejected; the panel stays A's.
    result, _ = await _run_guard(_callback(222, "apple_profile", message_id=557))
    assert result is None
    subject, panel_type = await PanelOwnershipService.get_panel_context(
        db_session, -100777, 557, fallback_user_id=222
    )
    assert subject == 111  # still A's panel
    assert panel_type == "profile"


@pytest.mark.asyncio
async def test_public_game_flows_stay_open_for_everyone(db_session):
    """Spec #6: B can still join/view/act in A's public game surfaces."""
    # A's party card is claimed (creator keeps cancel rights).
    await PanelOwnershipService.claim(
        db_session, -100777, 558, user_id=111, panel_type="panel_guard"
    )
    await db_session.commit()

    for data in ("join_party:12", "view_party:12", "group_games_panel"):
        result, called = await _run_guard(_callback(222, data, message_id=558))
        assert result == "HANDLED", f"B must pass on public action {data}"

    # Owner-scoped MUTATIONS on the same message still require ownership.
    result, called = await _run_guard(
        _callback(222, "create_party:rps:1v1", message_id=558)
    )
    assert result is None


@pytest.mark.asyncio
async def test_unowned_message_first_writer_wins_for_mutations(db_session):
    result, _ = await _run_guard(_callback(111, "create_party:rps:1v1"))
    assert result == "HANDLED"
    # Second writer on owner-scoped mutation is rejected (unchanged semantics).
    result, _ = await _run_guard(_callback(222, "create_party:rps:1v1"))
    assert result is None
    # But public actions pass on that same message.
    result, _ = await _run_guard(_callback(222, "join_party:3"))
    assert result == "HANDLED"
