"""Defense-in-depth regression tests for panel/private-callback authorization.

Proves the security rule: a user must not be able to use another user's
private panel or callback to modify/control that user's private state.

Tests exercise BOTH the middleware boundary AND the new service-boundary
``resolve_personal_actor`` helper. Public multiplayer flows (join party,
fill bots, start friendly, game actions) must continue to work.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)

from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services.panel import (
    LOCKED_MESSAGE,
    REJECT_MESSAGE,
    PERSONAL_PANEL_TYPES,
    PanelAuthorizationError,
    PanelOwnershipService,
    is_owner_scoped_callback,
    is_personal_panel,
    resolve_personal_actor,
)


# ---------------------------------------------------------------------------
# Callback factory
# ---------------------------------------------------------------------------


def _cb(
    user_id: int,
    data: str,
    chat_id: int = -100777,
    message_id: int = 555,
    chat_type: str = "supergroup",
):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="کاربر")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type=chat_type, title="گروه")
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


# ---------------------------------------------------------------------------
# Classification sanity
# ---------------------------------------------------------------------------


def test_apple_battle_is_a_personal_panel_type():
    assert "apple_battle" in PERSONAL_PANEL_TYPES
    assert is_personal_panel("apple_battle")


def test_apple_battle_actions_are_owner_scoped():
    for data in ("ab_pick:7", "ab_find", "ab_cancel", "ab_change"):
        assert is_owner_scoped_callback(data), data
    # friendly host controls are owner-scoped too (creator-gated server-side)
    for data in ("friendly_fill_bots:3", "friendly_start:3", "make_friendly:3"):
        assert is_owner_scoped_callback(data), data


# ---------------------------------------------------------------------------
# Middleware: owner can use their own personal panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_can_operate_own_personal_panel(db_session):
    await PanelOwnershipService.claim(
        db_session, -100777, 700, user_id=111, panel_type="garden"
    )
    await db_session.commit()

    for data in ("garden_harvest:0", "garden_refresh", "main_menu", "garden_unlock"):
        result, called = await _run_guard(_cb(111, data, message_id=700))
        assert result == "HANDLED"
        assert len(called) == 1, f"owner must pass on {data}"


# ---------------------------------------------------------------------------
# Middleware: another user cannot press anything on a personal panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_user_cannot_use_personal_panel(db_session):
    panels = [
        ("profile", 800),
        ("garden", 801),
        ("cards", 802),
        ("shop", 803),
        ("trade", 804),
        ("card_panel", 805),
        ("apple_battle", 806),
    ]
    for ptype, mid in panels:
        await PanelOwnershipService.claim(
            db_session, -100777, mid, user_id=111, panel_type=ptype
        )
    await db_session.commit()

    probes = [
        ("garden_harvest:1", 801),
        ("garden_refresh", 801),
        ("card_panel:5", 805),
        ("shop_buy:3", 803),
        ("trade_refresh", 804),
        ("ab_pick:1", 806),
        ("ab_find", 806),
        ("ab_cancel", 806),
    ]
    for data, mid in probes:
        result, called = await _run_guard(_cb(222, data, message_id=mid))
        assert result is None, f"non-owner must be locked out of {data}"
        assert called == []


# ---------------------------------------------------------------------------
# Forwarded / copied callback on a different chat (no panel row): owner-scoped
# callbacks fall through first-writer-wins -> the presser acts on their own
# state. Personal-panel callbacks (ab_pick, garden_*, card_upgrade_do) must
# still be safe because the service boundary validates the actor.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forwarded_callback_different_chat_first_writer_claims(db_session):
    # B forwards A's message to a different chat. There is no panel row for
    # that (chat_id, message_id) so the middleware allows B to press. This
    # is fine: B acts on B's own state (enforced in handlers/services).
    cb = _cb(222, "create_party:rps:solo", chat_id=-100999, message_id=1)
    result, called = await _run_guard(cb)
    assert result == "HANDLED"
    # And B now owns that panel message in THAT chat.
    owner = await PanelOwnershipService.get_owner(db_session, -100999, 1)
    assert owner == 222


@pytest.mark.asyncio
async def test_manually_reconstructed_callback_on_owner_panel_rejected(db_session):
    """A manually built callback targeting an existing owner panel is rejected."""
    await PanelOwnershipService.claim(
        db_session, -100777, 701, user_id=111, panel_type="apple_battle"
    )
    await db_session.commit()
    # User 222 crafts an ab_pick:999 (random card) targeting A's apple_battle panel
    cb = _cb(222, "ab_pick:999", message_id=701)
    result, called = await _run_guard(cb)
    assert result is None
    assert called == []
    cb.answer.assert_called_once()
    assert LOCKED_MESSAGE in cb.answer.call_args[0][0]


# ---------------------------------------------------------------------------
# Stale cross-user callback: the panel was closed/opened by someone else.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_callback_after_panel_changeover_is_rejected(db_session):
    """After A releases and B claims a public menu, A cannot use B's mutations."""
    await PanelOwnershipService.claim(
        db_session, -100777, 702, user_id=222, panel_type="panel_guard"
    )
    await db_session.commit()
    cb = _cb(111, "select_game:tic_tac_toe", message_id=702)
    result, called = await _run_guard(cb)
    assert result is None
    assert called == []
    cb.answer.assert_called_once()
    assert REJECT_MESSAGE in cb.answer.call_args[0][0]


# ---------------------------------------------------------------------------
# Public party flows still work for non-owners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_party_join_still_works_for_other_users(db_session):
    # A created a party (party card claimed as panel_guard).
    await PanelOwnershipService.claim(
        db_session, -100777, 703, user_id=111, panel_type="panel_guard"
    )
    await db_session.commit()
    # B can still join / view / accept match / reject.
    for data in (
        "join_party:12",
        "view_party:12",
        "accept_match:1:1",
        "reject_match:1",
        "group_games_panel",
        "group_active_games",
        "game_refresh:1",
        "ttt_action:1:4",  # game moves are public per the engine's own player check
        "trade_join:111:5",  # joining a trade offer is public
    ):
        result, called = await _run_guard(_cb(222, data, message_id=703))
        assert result == "HANDLED", f"B must be able to press public action {data}"


# ---------------------------------------------------------------------------
# Friendly-party host controls (fill with bots / start / make friendly) are
# restricted to the host at the MIDDLEWARE level. Server-side the PartyService
# also checks creator_user_id (defense in depth).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_host_cannot_fill_bots_or_start_friendly(db_session):
    await PanelOwnershipService.claim(
        db_session, -100777, 704, user_id=111, panel_type="panel_guard"
    )
    await db_session.commit()

    for data in ("friendly_fill_bots:5", "friendly_start:5", "make_friendly:5"):
        cb = _cb(222, data, message_id=704)
        result, called = await _run_guard(cb)
        assert result is None, f"non-host must not press {data}"
        assert called == []


@pytest.mark.asyncio
async def test_host_can_use_friendly_controls(db_session):
    await PanelOwnershipService.claim(
        db_session, -100777, 705, user_id=111, panel_type="panel_guard"
    )
    await db_session.commit()

    for data in ("friendly_fill_bots:5", "friendly_start:5", "make_friendly:5"):
        cb = _cb(111, data, message_id=705)
        result, called = await _run_guard(cb)
        assert result == "HANDLED", f"host must be able to press {data}"


# ---------------------------------------------------------------------------
# resolve_personal_actor: service-boundary unit behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_personal_actor_rejects_non_owner(db_session):
    await PanelOwnershipService.claim(
        db_session, -100777, 710, user_id=111, panel_type="garden"
    )
    await db_session.commit()

    cb = _cb(222, "garden_harvest:0", message_id=710)
    with pytest.raises(PanelAuthorizationError):
        await resolve_personal_actor(cb)


@pytest.mark.asyncio
async def test_resolve_personal_actor_returns_owner_for_owner(db_session):
    await PanelOwnershipService.claim(
        db_session, -100777, 711, user_id=111, panel_type="apple_battle"
    )
    await db_session.commit()

    cb = _cb(111, "ab_find", message_id=711)
    actor, _mid = await resolve_personal_actor(cb)
    assert actor == 111


@pytest.mark.asyncio
async def test_resolve_personal_actor_falls_back_to_presser_on_public_message(db_session):
    # Public (un-owned) message: presser acts on their own state.
    cb = _cb(222, "main_menu", message_id=712)
    actor, mid = await resolve_personal_actor(cb)
    assert actor == 222
    assert mid == 712


@pytest.mark.asyncio
async def test_resolve_personal_actor_no_message_returns_presser():
    cb = _cb(222, "noop")
    cb.message = None
    actor, mid = await resolve_personal_actor(cb)
    assert actor == 222
    assert mid is None
