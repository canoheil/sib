"""Inline-keyboard ownership for «معدن سیبی».

Opening the mine menu bakes the opener's Telegram id into every button.
Telegram does not restrict who can press an inline button, so a click from
anyone else must be answered with an alert and must not read or mutate any
mine/wallet state.

Audit note: garden, shop, cards, chests and profile already enforce this via
``PanelOwnershipService`` / ``resolve_personal_actor`` (plus the
``PanelGuardMiddleware``); the mine is the only personal panel that was not
owner-scoped.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from sib.database.models import (
    AppleMineShift,
    AppleTransaction,
    AppleWallet,
    User,
    UserPickaxe,
)
from sib.handlers import apple_mine as mine_handler
from sib.services import apple_mine as mine_service
from sib.services.apple_wallet import AppleWalletService
from sib.services.telegram_ui import mine_callback_data

USER_A = 7101
USER_B = 7102
ALERT = "⚠️ این دکمه برای شما نیست!"

MINE_HANDLERS = {
    "mine:work": mine_handler.handle_mine_work,
    "mine:pickaxe": mine_handler.handle_mine_pickaxe,
    "mine:pickaxe:upgrade": mine_handler.handle_mine_pickaxe_upgrade,
    "mine:pickaxe:back": mine_handler.handle_mine_pickaxe_back,
}


def _session_context(session, opened=None):
    @asynccontextmanager
    async def _context():
        if opened is not None:
            opened.append(True)
        yield session

    return _context


def _message(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=-700, type="private"),
        answer_photo=AsyncMock(),
        answer=AsyncMock(),
    )


def _callback(data: str, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-700, type="private"),
            message_id=42,
            photo=[object()],
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


def _button_data(keyboard) -> set[str]:
    return {button.callback_data for row in keyboard.inline_keyboard for button in row}


def _all_mine_buttons(user_id: int) -> set[str]:
    """Every callback the real mine screens can put in front of a user."""
    return _button_data(mine_handler._main_keyboard(user_id)) | _button_data(
        mine_handler._pickaxe_keyboard(SimpleNamespace(level=1), user_id)
    )


async def _snapshot(session):
    """Balances, shifts, ledger rows and pickaxe levels for both users."""
    balances = []
    for user_id in (USER_A, USER_B):
        balances.append(await AppleWalletService.get_balance(session, user_id))
    balances = tuple(balances)
    shifts = (await session.execute(sa.select(AppleMineShift))).scalars().all()
    ledger = (await session.execute(sa.select(AppleTransaction))).scalars().all()
    pickaxes = (await session.execute(sa.select(UserPickaxe))).scalars().all()
    return (
        balances,
        tuple((row.user_id, row.amount) for row in shifts),
        tuple((row.user_id, row.amount) for row in ledger),
        sorted((row.user_id, row.level) for row in pickaxes),
    )


def _prepare_users(session):
    session.add(User(id=USER_A, first_name="الف"))
    session.add(User(id=USER_B, first_name="ب"))
    session.add(AppleWallet(user_id=USER_A, balance=1000))
    session.add(AppleWallet(user_id=USER_B, balance=25))


def _patch_handler_db(monkeypatch, session, opened):
    monkeypatch.setattr(mine_handler, "get_session", _session_context(session, opened))
    monkeypatch.setattr(
        mine_handler, "_register_user_and_group", AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_other_user_cannot_dig_from_owner_menu(db_session, monkeypatch):
    """User A opens the menu; User B presses Dig -> alert, zero state change."""
    _prepare_users(db_session)
    await db_session.flush()

    opened = []
    _patch_handler_db(monkeypatch, db_session, opened)
    perform = AsyncMock()
    monkeypatch.setattr(mine_handler, "perform_shift", perform)
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "safe_edit_message_caption", edit)

    # User A sends «معدن سیبی» and gets the owner-bound keyboard.
    message = _message(USER_A)
    await mine_handler.handle_mine_command(message)
    keyboard = message.answer_photo.await_args.kwargs["reply_markup"]
    dig = next(
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("mine:work")
    )
    assert dig == mine_callback_data("mine:work", USER_A)
    assert _button_data(keyboard) <= _all_mine_buttons(USER_A)

    before = await _snapshot(db_session)
    opened.clear()

    # User B clicks A's Dig button.
    callback = _callback(dig, USER_B)
    await mine_handler.handle_mine_work(callback, SimpleNamespace())

    callback.answer.assert_awaited_once_with(ALERT, show_alert=True)
    assert opened == []  # no DB session was even opened
    perform.assert_not_awaited()  # no shift, no payout
    edit.assert_not_awaited()  # A's panel was not edited
    callback.message.answer.assert_not_awaited()
    assert await _snapshot(db_session) == before


@pytest.mark.asyncio
async def test_owner_can_still_dig_from_own_menu(db_session, monkeypatch):
    """The same click by User A still runs a normal, paid shift."""
    _prepare_users(db_session)
    await db_session.flush()

    _patch_handler_db(monkeypatch, db_session, [])
    monkeypatch.setattr(mine_handler, "safe_edit_message_caption", AsyncMock(return_value=True))
    # Deterministic normal reward, no optional plant/disease side effects.
    monkeypatch.setattr(mine_service, "_plant_drop_chance", AsyncMock(return_value=0))
    monkeypatch.setattr(mine_service, "_mine_infection_chance", AsyncMock(return_value=0))
    monkeypatch.setattr(mine_service.random, "random", lambda: 0.9999)
    monkeypatch.setattr(mine_service.random, "randint", lambda a, b: b)

    message = _message(USER_A)
    await mine_handler.handle_mine_command(message)
    keyboard = message.answer_photo.await_args.kwargs["reply_markup"]
    dig = next(
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("mine:work")
    )

    callback = _callback(dig, USER_A)
    await mine_handler.handle_mine_work(callback, SimpleNamespace())

    callback.answer.assert_awaited_once_with(None, show_alert=False)
    shifts = (
        await db_session.execute(
            sa.select(AppleMineShift).where(AppleMineShift.user_id == USER_A)
        )
    ).scalars().all()
    assert len(shifts) == 1
    assert shifts[0].amount > 0
    assert await AppleWalletService.get_balance(db_session, USER_A) == 1000 + shifts[0].amount
    assert await AppleWalletService.get_balance(db_session, USER_B) == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("action, handler", sorted(MINE_HANDLERS.items()))
async def test_every_mine_button_rejects_foreign_click(monkeypatch, action, handler):
    """Work, pickaxe screen, upgrade and back are all owner-scoped."""
    data = mine_callback_data(action, USER_A)
    assert data in _all_mine_buttons(USER_A)

    opened = []
    monkeypatch.setattr(mine_handler, "get_session", _session_context(object(), opened))
    mutations = []
    for name in (
        "_register_user_and_group",
        "_read_mine_view",
        "remaining_cooldown",
        "perform_shift",
        "upgrade_pickaxe",
    ):
        mock = AsyncMock()
        monkeypatch.setattr(mine_handler, name, mock)
        mutations.append(mock)
    render = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "_edit_screen", render)

    callback = _callback(data, USER_B)
    await handler(callback, SimpleNamespace())

    callback.answer.assert_awaited_once_with(ALERT, show_alert=True)
    assert opened == []
    for mock in mutations:
        mock.assert_not_awaited()
    render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data, user_id",
    [
        ("mine:work", USER_A),  # stale panel with no owner id
        ("mine:pickaxe:upgrade", USER_A),
        ("mine:work:", USER_A),  # empty owner id
        ("mine:work:abc", USER_A),  # malformed owner id
        (mine_callback_data("mine:work", USER_B), USER_A),  # forged owner
    ],
)
async def test_stale_or_forged_mine_callbacks_are_rejected(monkeypatch, data, user_id):
    handler = MINE_HANDLERS[
        "mine:pickaxe:upgrade" if data.startswith("mine:pickaxe:upgrade") else "mine:work"
    ]
    opened = []
    monkeypatch.setattr(mine_handler, "get_session", _session_context(object(), opened))
    monkeypatch.setattr(mine_handler, "_edit_screen", AsyncMock(return_value=True))
    monkeypatch.setattr(mine_handler, "perform_shift", AsyncMock())
    monkeypatch.setattr(mine_handler, "upgrade_pickaxe", AsyncMock())

    callback = _callback(data, user_id)
    await handler(callback, SimpleNamespace())

    callback.answer.assert_awaited_once_with(ALERT, show_alert=True)
    assert opened == []


def test_all_mine_buttons_carry_the_owner_id():
    """No mine button may be pressable without an owner check."""
    assert _all_mine_buttons(USER_A) == {
        mine_callback_data(action, USER_A) for action in MINE_HANDLERS
    }
    assert _all_mine_buttons(USER_A).isdisjoint(_all_mine_buttons(USER_B))


@pytest.mark.parametrize("action, handler", sorted(MINE_HANDLERS.items()))
def test_owner_bound_callbacks_still_reach_their_handler(action, handler):
    """The new data shape must not silently drop the callback (no answer)."""
    data = mine_callback_data(action, USER_A)
    matched = [
        registered.callback.__name__
        for registered in mine_handler.router.callback_query.handlers
        if all(
            flt.callback(value=SimpleNamespace(data=data))
            for flt in registered.filters
        )
    ]
    assert matched == [handler.__name__]
