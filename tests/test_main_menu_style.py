"""Main Menu — button styling, on the one menu each chat type now has.

Scoped rule: the Games entry of the GROUP main menu («🎮 بازی‌ها») uses the
green (success) glass style; every other button keeps its original style.

The private menu no longer has a «🎮 بازی‌ها و راهنما» entry: that button was
part of the retired second private menu and, despite the label, only opened the
guide. Private chat now renders exactly one menu (``get_private_start_keyboard``,
the /start home screen), where the guide is «📖 راهنما» and games live in groups
— where the group menu's green Games entry leads.
"""

from sib.keyboards.common import (
    get_group_welcome_keyboard,
    get_private_start_keyboard,
)


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_games_button_is_green_in_group_main_menu():
    kb = get_group_welcome_keyboard()
    games = [b for b in _buttons(kb) if (b.text or "").startswith("🎮")]
    assert len(games) == 1
    assert games[0].callback_data == "group_games_panel"
    assert games[0].style == "success"  # green glass


def test_private_menu_has_the_guide_but_no_games_entry():
    """Games are a group feature; the private menu must not pretend otherwise."""
    kb = get_private_start_keyboard("sib_bot")
    buttons = _buttons(kb)

    assert not [b for b in buttons if (b.text or "").startswith("🎮")]
    guide = [b for b in buttons if b.callback_data == "help_menu"]
    assert len(guide) == 1
    assert guide[0].text == "📖 راهنما"


def test_other_main_menu_buttons_keep_their_styles():
    """Only the Games buttons are green — nothing else in these menus moves."""
    for kb in (get_group_welcome_keyboard(), get_private_start_keyboard("sib_bot")):
        for b in _buttons(kb):
            if (b.text or "").startswith("🎮"):
                continue
            assert b.style in ("primary", "success", "danger", None), b.text

    group_kb = get_group_welcome_keyboard()
    by_text = {b.text: b for b in _buttons(group_kb)}
    # Every non-games entry of the reorganized group menu stays blue
    # (the chest entry included).
    assert by_text["📖 راهنما"].style == "primary"
    assert by_text["🛒 فروشگاه"].style == "primary"
    assert by_text["👤 پروفایل من"].style == "primary"
    assert by_text["🏆 رتبه و آمار گروه"].style == "primary"
    assert by_text["🎁 صندوقچه گنج روزانه"].style == "primary"

    private_kb = get_private_start_keyboard("sib_bot")
    private_by_text = {b.text: b for b in _buttons(private_kb)}
    # The chest is the one deliberately highlighted action of the private menu.
    assert private_by_text["🎁 صندوق روزانه"].style == "success"
    for label in ("🌳 باغ من", "🎴 کارت‌های من", "🛒 فروشگاه", "👤 پروفایل من"):
        assert private_by_text[label].style == "primary", label
