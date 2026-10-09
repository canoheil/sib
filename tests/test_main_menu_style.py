"""Main Menu — Games button styling.

Scoped change: the Games entry of BOTH main menus (group «🎮 بازی‌ها» and
private «🎮 بازی‌ها و راهنما») uses the green (success) glass style. Every
other button keeps its original style — the shared button helpers are not
touched by the change.
"""

from sib.keyboards.common import (
    get_group_welcome_keyboard,
    get_private_welcome_keyboard,
)


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_games_button_is_green_in_group_main_menu():
    kb = get_group_welcome_keyboard()
    games = [b for b in _buttons(kb) if (b.text or "").startswith("🎮")]
    assert len(games) == 1
    assert games[0].callback_data == "group_games_panel"
    assert games[0].style == "success"  # green glass


def test_games_button_is_green_in_private_main_menu():
    kb = get_private_welcome_keyboard("sib_bot")
    games = [b for b in _buttons(kb) if (b.text or "").startswith("🎮")]
    assert len(games) == 1
    assert games[0].callback_data == "help_menu"
    assert games[0].style == "success"  # green glass


def test_other_main_menu_buttons_keep_their_styles():
    """Only the Games buttons changed — nothing else in these menus moves."""
    for kb in (get_group_welcome_keyboard(), get_private_welcome_keyboard("sib_bot")):
        for b in _buttons(kb):
            if (b.text or "").startswith("🎮"):
                continue
            assert b.style in ("primary", "danger", None), b.text

    group_kb = get_group_welcome_keyboard()
    by_text = {b.text: b for b in _buttons(group_kb)}
    # Every non-games entry of the reorganized 6-button menu stays blue
    # (the chest entry included).
    assert by_text["📖 راهنما"].style == "primary"
    assert by_text["🛒 فروشگاه"].style == "primary"
    assert by_text["👤 پروفایل من"].style == "primary"
    assert by_text["🏆 رتبه و آمار گروه"].style == "primary"
