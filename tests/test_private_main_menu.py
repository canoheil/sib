"""One private menu: «🏠 منوی اصلی» must land on the screen /start shows.

The bug this file pins down: private chat used to render TWO main menus.

* ``/start`` (and the post-membership-check screen) rendered the real
  first-session menu: chest, garden, cards, shop, profile, guide, add-to-group.
* Every «🏠 منوی اصلی» back button — the guide, the shop, the garden, the
  cards screen, the wiki, the chest — rendered a *retired* menu built by
  ``get_private_welcome_keyboard``: wiki and card trading at the top level, a
  «🎮 بازی‌ها و راهنما» button that opened the guide, no daily-chest entry
  ordering, and no agreement with /start anywhere else in the bot.

So a player who opened the guide and pressed «منوی اصلی» was dropped into a
menu the game no longer opens — exactly the "old version" feeling that was
reported. The old builder is deleted; these tests make sure the single source
of truth stays single.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sib.core.config import settings
from sib.handlers.start import _main_menu_text_and_kb, handle_main_menu
from sib.keyboards.common import (
    get_group_welcome_keyboard,
    get_private_start_keyboard,
    get_profile_keyboard,
    get_cards_keyboard,
)

PRIVATE_CHAT = SimpleNamespace(id=4242, type="private")
GROUP_CHAT = SimpleNamespace(id=-100123, type="supergroup")


def _user(first_name="سارا"):
    return SimpleNamespace(id=4242, first_name=first_name)


def _shape(kb):
    """A comparable snapshot of a keyboard (text, target and style)."""
    return [
        [(b.text, b.callback_data, b.url, b.style) for b in row]
        for row in kb.inline_keyboard
    ]


def _callback_data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #
def test_private_main_menu_is_the_start_menu():
    _text, kb = _main_menu_text_and_kb(_user(), PRIVATE_CHAT)

    assert _shape(kb) == _shape(get_private_start_keyboard(settings.BOT_USERNAME))


def test_private_main_menu_does_not_carry_the_channel_gate_row():
    """Navigation never spends a chat-member API call to draw the menu."""
    _text, kb = _main_menu_text_and_kb(_user(), PRIVATE_CHAT)

    callbacks = _callback_data(kb)
    assert "check_membership" not in callbacks
    # ...while the /start entry point can still offer it.
    gated = get_private_start_keyboard(
        settings.BOT_USERNAME, gate_channel_link="https://t.me/sib_channel"
    )
    assert "check_membership" in _callback_data(gated)


def test_group_main_menu_is_untouched():
    _text, kb = _main_menu_text_and_kb(_user(), GROUP_CHAT)

    assert _shape(kb) == _shape(get_group_welcome_keyboard())


def test_the_retired_menu_entries_are_gone_from_the_top_level():
    """Wiki and card trading moved into the screens that own them."""
    _text, kb = _main_menu_text_and_kb(_user(), PRIVATE_CHAT)
    callbacks = _callback_data(kb)

    assert "apple_wiki" not in callbacks
    assert "apple_trade" not in callbacks
    # Nothing was orphaned: both are one tap away from their real home
    # (they carry the screen they were opened from as their back target).
    cards = _callback_data(get_cards_keyboard())
    assert any(c and c.startswith("apple_wiki") for c in cards)
    profile = _callback_data(get_profile_keyboard())
    assert any(c and c.startswith("apple_trade") for c in profile)


def test_the_private_menu_lists_every_first_session_action():
    _text, kb = _main_menu_text_and_kb(_user(), PRIVATE_CHAT)
    callbacks = _callback_data(kb)

    for expected in (
        "apple_chest",
        "apple_garden",
        "apple_cards",
        "apple_shop",
        "apple_profile",
        "help_menu",
    ):
        assert expected in callbacks, expected


# --------------------------------------------------------------------------- #
# The button
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_back_to_main_menu_renders_that_same_menu():
    callback = SimpleNamespace(
        from_user=_user(),
        message=SimpleNamespace(chat=PRIVATE_CHAT, message_id=1, photo=None),
    )
    bot = AsyncMock()

    with patch(
        "sib.services.banner.render_banner", new=AsyncMock(return_value=True)
    ) as render:
        await handle_main_menu(callback, bot)

    assert render.await_count == 1
    _callback_arg, _bot_arg, key, text, keyboard = render.await_args.args
    assert key == "main_menu"
    assert _shape(keyboard) == _shape(get_private_start_keyboard(settings.BOT_USERNAME))
    # The greeting survives, so the screen still feels like "home".
    assert "منوی اصلی سیب" in text
    assert "سارا" in text


@pytest.mark.asyncio
async def test_back_to_main_menu_in_a_group_renders_the_group_menu():
    callback = SimpleNamespace(
        from_user=_user(),
        message=SimpleNamespace(chat=GROUP_CHAT, message_id=2, photo=None),
    )

    with patch(
        "sib.services.banner.render_banner", new=AsyncMock(return_value=True)
    ) as render:
        await handle_main_menu(callback, AsyncMock())

    keyboard = render.await_args.args[4]
    assert _shape(keyboard) == _shape(get_group_welcome_keyboard())


def test_the_guide_back_button_targets_the_main_menu():
    """The reported path: guide -> «🏠 منوی اصلی» must hit THIS handler."""
    from sib.handlers.start import _help_keyboard

    back = [
        button
        for row in _help_keyboard().inline_keyboard
        for button in row
        if button.callback_data == "main_menu"
    ]
    assert len(back) == 1


# --------------------------------------------------------------------------- #
# One source of truth (the retired builder must never come back)
# --------------------------------------------------------------------------- #
def test_the_legacy_private_menu_builder_is_deleted():
    import sib.keyboards.common as common

    assert not hasattr(common, "get_private_welcome_keyboard")

    source = inspect.getsource(common)
    assert "def get_private_welcome_keyboard" not in source


def test_the_private_menu_has_exactly_one_builder():
    """No other module may hand-build a private main menu."""
    import sib.keyboards.common as common

    source = inspect.getsource(common)
    assert source.count("def get_private_start_keyboard") == 1
    # The add-to-group URL is the signature of a private menu builder; it must
    # appear in exactly one place, so a second private menu cannot quietly
    # reappear next to this one.
    assert source.count("?startgroup=true") == 1
