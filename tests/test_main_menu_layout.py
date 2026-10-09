"""Reorganized main menu: 6 entries, one entry per feature.

The group main menu keeps exactly six options, in this order:

    [ 🎮 بازی‌ها ]                            (full width, first row)
    [ 📖 راهنما ]        [ 🛒 فروشگاه ]
    [ 👤 پروفایل من ]    [ 🏆 رتبه و آمار گروه ]
    [ 🎁 صندوقچه گنج روزانه ]                  (full width, last row)

Card Trading, My Cards and Active Group Games are NOT duplicated in the
main menu - each keeps its ONE canonical entry:

    Card Trading / My Cards -> «👤 پروفایل من»
    Active Group Games      -> «🎮 بازی‌ها» (games menu)

Removing a shortcut only changes where a feature is reached from: the
callbacks and handlers stay untouched.
"""

from sib.keyboards.common import (
    get_group_welcome_keyboard,
    get_profile_keyboard,
)
from sib.keyboards.party import (
    get_active_parties_keyboard,
    get_games_menu_keyboard,
)


def _buttons(kb):
    return [button for row in kb.inline_keyboard for button in row]


def _rows(kb):
    """Buttons of every row as (text, callback_data) pairs."""
    return [
        [(button.text, button.callback_data) for button in row]
        for row in kb.inline_keyboard
    ]


# ---------------------------------------------------------------------------
# The 6-option main menu
# ---------------------------------------------------------------------------

def test_group_main_menu_has_exactly_six_options():
    main_menu = _buttons(get_group_welcome_keyboard())
    assert len(main_menu) == 6


def test_group_main_menu_order_is_games_help_shop_profile_rank_chest():
    callbacks = [button.callback_data for button in _buttons(get_group_welcome_keyboard())]
    assert callbacks == [
        "group_games_panel",  # 1. Games
        "help_menu",          # 2. Help
        "apple_shop",         # 3. Shop
        "apple_profile",      # 4. My Profile
        "group_rating_stats", # 5. Group Rank & Stats
        "group_chest",        # 6. Daily Treasure Chest (opens in this group)
    ]


def test_games_button_is_full_width_on_its_own_first_row():
    rows = _rows(get_group_welcome_keyboard())
    games_row = rows[0]
    assert len(games_row) == 1  # a single button spans the whole row
    assert games_row[0][1] == "group_games_panel"
    # Help/Shop and Profile/Rank are two rows of two buttons; the chest closes
    # the menu on its own full-width row.
    assert [len(row) for row in rows] == [1, 2, 2, 1]


def test_removed_duplicates_are_not_in_the_main_menu():
    callbacks = {button.callback_data for button in _buttons(get_group_welcome_keyboard())}
    # These moved to their canonical screens - never duplicated here.
    assert "apple_trade" not in callbacks       # Card Trading -> Profile
    assert "apple_cards" not in callbacks       # My Cards     -> Profile
    assert "group_active_games" not in callbacks  # Active Games -> Games menu
    # Apple Battle is NOT a main-menu entry either (its home is the games
    # menu); its functionality is untouched.
    assert not any((cb or "").startswith("apple_battle") for cb in callbacks)


# ---------------------------------------------------------------------------
# The relocated features stay one tap away
# ---------------------------------------------------------------------------

def test_profile_keeps_my_cards_and_adds_card_trading():
    keys = {button.callback_data: button for button in _buttons(get_profile_keyboard())}

    # My Cards stays in the profile (contextual Back to the profile).
    assert "apple_cards:apple_profile" in keys
    # Card Trading is reachable from the profile - and opens its hub with a
    # Back to the profile (handler `apple_trade` is unchanged).
    assert "apple_trade:apple_profile" in keys
    # The rest of the profile functionality is untouched.
    assert "apple_garden" in keys
    assert "apple_shop" in keys
    assert "main_menu" in keys  # Back to the main menu still works


def test_games_menu_keeps_active_group_games_and_apple_battle():
    keys = {
        button.callback_data: button
        for button in _buttons(get_games_menu_keyboard([], active_count=0))
    }
    assert "group_active_games" in keys  # Active Group Games stays here
    # Apple Battle keeps its single canonical entry.
    assert "apple_battle:group_games_panel" in keys


def test_active_games_screen_has_no_apple_battle_button():
    """Active Group Games (empty state): New Game / Refresh / Back to Games."""
    kb = get_active_parties_keyboard([])
    rows = [[button.callback_data for button in row] for row in kb.inline_keyboard]
    assert rows == [
        ["create_new_party_menu", "group_active_games"],
        ["group_games_panel"],
    ]
    labels = " | ".join(button.text or "" for button in _buttons(kb))
    assert "ساخت بازی جدید" in labels and "بروزرسانی" in labels and "بازگشت" in labels
    # No Apple Battle shortcut on this screen - but its feature still lives
    # in the games menu (asserted above).
    assert not any(
        (button.callback_data or "").startswith("apple_battle")
        for button in _buttons(kb)
    )
