"""Routing regression: friendship request buttons must reach the friendship
handlers — never the admin guard.

The admin routers carry a router-level ``IsAdminFilter`` and were registered
before the friendship router, so every «✅ قبول میکنم» click by a non-admin
logged a bogus "Unauthorized admin access attempt" (and risked being
swallowed before reaching the friendship handler). The friendship router is
now included BEFORE the admin routers, and the filter stays silent for
non-admin surfaces.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser

from sib.core.config import settings
from sib.handlers import register_all_handlers
from sib.handlers import friendship
from sib.keyboards.friendship import FRIEND_ACCEPT, FRIEND_REJECT

NON_ADMIN_ID = 42


def _friend_callback_update(data: str, user_id: int = NON_ADMIN_ID) -> Update:
    """A group callback query exactly like the one Telegram sends when the
    user presses «✅ قبول میکنم» / «❌ رد میکنم» on a request card."""
    user = TelegramUser(id=user_id, first_name="player", is_bot=False)
    chat = Chat(id=-100123, type="supergroup")
    msg = Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=TelegramUser(id=1, first_name="sib", is_bot=True),
    )
    cb = CallbackQuery(
        id="cbq-1",
        chat_instance="chat-instance",
        from_user=user,
        message=msg,
        data=data,
    )
    return Update(update_id=1, callback_query=cb)


@pytest.fixture(scope="module")
def dp():
    """One dispatcher with every handler registered (routers can only be
    attached to a single dispatcher, so it is shared by the whole module)."""
    dispatcher = Dispatcher()
    register_all_handlers(dispatcher)
    return dispatcher


@pytest.fixture
def non_admin(monkeypatch):
    """The clicker is NOT in ADMIN_IDS."""
    monkeypatch.setattr(settings, "ADMIN_IDS", [999_999])


@pytest.mark.parametrize(
    ("data", "action"),
    [
        (f"{FRIEND_ACCEPT}:123", FRIEND_ACCEPT),  # «✅ قبول میکنم»
        (f"{FRIEND_REJECT}:123", FRIEND_REJECT),  # «❌ رد میکنم»
    ],
)
async def test_friend_button_reaches_friendship_handler_without_admin_warning(
    monkeypatch, caplog, non_admin, dp, data, action
):
    """Clicking a friendship button reaches the friendship accept/reject
    handler (not silently ignored) and does NOT trigger the admin warning."""
    spy = AsyncMock()
    monkeypatch.setattr(friendship, "_resolve_friend_callback", spy)

    caplog.set_level(logging.WARNING)
    await dp.feed_update(SimpleNamespace(id=1), _friend_callback_update(data))

    # The friendship handler ran with the right action...
    spy.assert_awaited_once()
    assert spy.await_args.args[0].data == data
    assert spy.await_args.args[1] == action
    # ...and no admin guard complained about the click.
    assert not [r for r in caplog.records if "Unauthorized admin" in r.getMessage()]


def test_friendship_router_is_registered_before_the_admin_routers(dp):
    """The friendship router must come before every admin router that carries
    the IsAdminFilter, so friend callbacks never hit the admin guard."""
    from sib.handlers import (
        admin as admin_module,
        admin_center,
        admin_daily_digest,
        admin_meta,
    )

    sub_routers = dp.sub_routers
    friendship_index = next(
        i for i, r in enumerate(sub_routers) if r is friendship.router
    )
    for admin_router in (
        admin_module.router,
        admin_center.router,
        admin_meta.router,
        admin_daily_digest.router,
    ):
        admin_index = next(i for i, r in enumerate(sub_routers) if r is admin_router)
        assert friendship_index < admin_index
