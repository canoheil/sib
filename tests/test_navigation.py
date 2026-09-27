"""Navigation consistency tests (bug report requirements #5/#10).

Contract:
- 'Back' navigation NEVER cancels an active match/party (no cancel
  callbacks behind plain navigation buttons).
- Panels use the shared nav helpers with consistent Persian labels.
- Every board keeps its Refresh (recovery) entry point.
"""

from unittest.mock import MagicMock

from sib.keyboards.common import nav_back_row, nav_refresh_row
from sib.keyboards.party import (
    get_active_parties_keyboard,
    get_game_mode_keyboard,
    get_game_selection_keyboard,
    get_party_action_keyboard,
)

CANCEL_PREFIXES = ("cancel_party", "match_cancel", "gs_cancel", "leave_party")
NAV_BACK_LABELS = ("🔙", "بازگشت")


def _all_buttons(kb):
    return [btn for row in kb.inline_keyboard for btn in row]


def _game(code="tic_tac_toe"):
    g = MagicMock()
    g.code = code
    g.name_fa = "بازی"
    g.supports_solo = True
    g.supports_2v2 = True
    g.supports_4v4 = False
    return g


def test_nav_helpers_build_single_button_rows():
    back = nav_back_row("🔙 بازگشت", "group_games_panel")
    refresh = nav_refresh_row("🔄 بروزرسانی", "game_refresh:1")
    assert len(back) == 1 and len(back[0]) == 1
    assert back[0][0].callback_data == "group_games_panel"
    assert refresh[0][0].callback_data == "game_refresh:1"


def test_back_buttons_never_cancel_matches_or_parties():
    """Plain navigation must never be wired to a destructive action."""
    party = MagicMock()
    party.id = 7
    party.is_full = False
    party.mode = "solo"

    keyboards = [
        get_game_selection_keyboard([_game()]),
        get_game_mode_keyboard(_game()),
        get_party_action_keyboard(party, is_member=True),
        get_active_parties_keyboard([]),
    ]
    for kb in keyboards:
        for btn in _all_buttons(kb):
            is_back = any(label in (btn.text or "") for label in NAV_BACK_LABELS)
            if is_back:
                assert not btn.callback_data.startswith(CANCEL_PREFIXES), (
                    f"Back button '{btn.text}' is wired to {btn.callback_data}"
                )
                # Back only navigates to known safe roots.
                assert btn.callback_data in (
                    "group_games_panel",
                    "close_panel",
                    "admin_main",
                )


def test_party_panels_have_back_and_refresh_entries():
    party = MagicMock()
    party.id = 7
    party.is_full = False
    party.mode = "solo"

    mode_kb = get_game_mode_keyboard(_game())
    labels = [b.text for b in _all_buttons(mode_kb)]
    assert any("بازگشت" in t for t in labels)

    party_kb = get_party_action_keyboard(party, is_member=True)
    labels = [b.text for b in _all_buttons(party_kb)]
    assert any("بروزرسانی" in t for t in labels)

    list_kb = get_active_parties_keyboard([])
    labels = [b.text for b in _all_buttons(list_kb)]
    assert any("بروزرسانی" in t for t in labels)


def test_board_refresh_button_exists_in_rematch_keyboard():
    from sib.handlers.games.common import get_rematch_keyboard, with_refresh_button

    kb = with_refresh_button(get_rematch_keyboard(), match_id=42)
    callbacks = [b.callback_data for b in _all_buttons(kb)]
    assert "game_refresh:42" in callbacks
    # Rematch entry point stays non-destructive.
    assert "group_games_panel" in callbacks
