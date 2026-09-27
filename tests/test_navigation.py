"""Navigation consistency tests (task spec: complete navigation overhaul).

Contract:
- The chain main menu -> games menu -> active games -> one game works with
  buttons only; every screen has a working Back button to its parent.
- Back NEVER cancels an active match/party (no cancel callbacks behind
  plain navigation buttons).
- Cards / shop / profile / apple battle / rating-stats all return to the
  main menu ('↩️ بازگشت به منوی اصلی'); rating-stats no longer has a
  panel-closing 'بستن' button.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sib.keyboards.common import (
    get_cards_keyboard,
    get_group_welcome_keyboard,
    get_profile_keyboard,
    get_private_welcome_keyboard,
    nav_back_row,
    nav_refresh_row,
)
from sib.keyboards.leaderboard import (
    get_group_stats_keyboard,
    get_leaderboard_keyboard,
)
from sib.keyboards.party import (
    get_active_parties_keyboard,
    get_game_mode_keyboard,
    get_game_selection_keyboard,
    get_games_menu_keyboard,
    get_party_action_keyboard,
)

CANCEL_PREFIXES = ("cancel_party", "match_cancel", "gs_cancel", "leave_party")
NAV_BACK_LABELS = ("🔙", "↩️", "بازگشت")
SAFE_NAV_TARGETS = (
    "main_menu",
    "group_games_panel",
    "group_active_games",
    "create_new_party_menu",
    "apple_cards",
    "close_panel",
    "admin_main",
    "wiki_page:0",
)


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


def _party(full=False):
    p = MagicMock()
    p.id = 7
    p.is_full = full
    p.mode = "solo"
    return p


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def test_nav_helpers_build_single_button_rows():
    back = nav_back_row("↩️ بازگشت به منوی اصلی", "main_menu")
    refresh = nav_refresh_row("🔄 بروزرسانی", "game_refresh:1")
    assert len(back) == 1 and len(back[0]) == 1
    assert back[0][0].callback_data == "main_menu"
    assert refresh[0][0].callback_data == "game_refresh:1"


def test_back_buttons_never_cancel_matches_or_parties():
    """Plain navigation must never be wired to a destructive action."""
    keyboards = [
        get_games_menu_keyboard([_game()], active_count=1),
        get_active_parties_keyboard([]),
        get_active_parties_keyboard([_party()]),
        get_game_selection_keyboard([_game()]),
        get_game_mode_keyboard(_game()),
        get_party_action_keyboard(_party(), is_member=True),
        get_party_action_keyboard(_party(full=True)),
        get_cards_keyboard(),
        get_profile_keyboard(),
        get_group_stats_keyboard(),
        get_leaderboard_keyboard(is_group=True),
    ]
    for kb in keyboards:
        for btn in _all_buttons(kb):
            is_back = any(label in (btn.text or "") for label in NAV_BACK_LABELS)
            if is_back:
                assert not btn.callback_data.startswith(CANCEL_PREFIXES), (
                    f"Back button '{btn.text}' is wired to {btn.callback_data}"
                )
                assert btn.callback_data.split(":")[0] in SAFE_NAV_TARGETS or (
                    btn.callback_data in SAFE_NAV_TARGETS
                ), f"Back button '{btn.text}' has unexpected target {btn.callback_data}"


# ---------------------------------------------------------------------------
# The context-aware chain (spec #2)
# ---------------------------------------------------------------------------

def test_games_menu_links_to_active_games_and_main():
    kb = get_games_menu_keyboard([_game()], active_count=2)
    callbacks = {b.callback_data: b for b in _all_buttons(kb)}
    assert "group_active_games" in callbacks
    assert "main_menu" in callbacks
    assert "select_game:tic_tac_toe" in callbacks
    # Active-games entry shows the live count.
    assert "(2)" in callbacks["group_active_games"].text


def test_active_games_links_back_to_games_menu():
    kb = get_active_parties_keyboard([_party()])
    callbacks = {b.callback_data: b for b in _all_buttons(kb)}
    assert "group_games_panel" in callbacks
    assert "view_party:7" in callbacks
    assert "create_new_party_menu" in callbacks
    assert "group_active_games" in callbacks  # refresh


def test_party_screen_links_back_to_active_games():
    for party in (_party(), _party(full=True)):
        kb = get_party_action_keyboard(party, is_member=True)
        callbacks = {b.callback_data for b in _all_buttons(kb)}
        assert "group_active_games" in callbacks
        # Back never cancels the party.
        assert f"cancel_party:{party.id}" not in {
            b.callback_data for b in _all_buttons(kb) if "بازگشت" in (b.text or "")
        }


def test_full_chain_is_walkable_with_buttons_only():
    """main -> games -> active -> party -> back -> back -> back == main."""
    main_kb = get_group_welcome_keyboard()
    main_cbs = {b.callback_data for b in _all_buttons(main_kb)}
    assert "group_games_panel" in main_cbs

    games_kb = get_games_menu_keyboard([_game()], active_count=0)
    games_cbs = {b.callback_data for b in _all_buttons(games_kb)}
    assert "group_active_games" in games_cbs

    active_kb = get_active_parties_keyboard([_party()])
    active_cbs = {b.callback_data for b in _all_buttons(active_kb)}
    assert "view_party:7" in active_cbs

    party_kb = get_party_action_keyboard(_party(), is_member=True)
    party_cbs = {b.callback_data for b in _all_buttons(party_kb)}
    assert "group_active_games" in party_cbs  # back one level

    # ... and the chain of Back buttons reaches the main menu again.
    assert "group_games_panel" in active_cbs  # back one level
    assert "main_menu" in games_cbs  # back one level


# ---------------------------------------------------------------------------
# Panels return to the main menu (spec #1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kb_factory",
    [
        get_cards_keyboard,
        get_profile_keyboard,
        get_group_stats_keyboard,
    ],
)
def test_panels_have_back_to_main(kb_factory):
    kb = kb_factory()
    buttons = _all_buttons(kb)
    back = [b for b in buttons if "بازگشت به منوی اصلی" in (b.text or "")]
    assert back, f"{kb_factory.__name__} must have a Back to main menu button"
    assert all(b.callback_data == "main_menu" for b in back)


def test_group_stats_no_longer_closes_the_panel():
    """The old 'بستن' button must be replaced by a Back to main menu."""
    for kb in (get_group_stats_keyboard(), get_leaderboard_keyboard(is_group=True)):
        buttons = _all_buttons(kb)
        assert not any(
            (b.text or "") == "🔙 بستن" or b.callback_data == "close_panel" for b in buttons
        ), "stats panels must not fully close themselves"


def test_shop_and_battle_and_garden_and_trade_have_back_to_main():
    from sib.handlers.apple_battle import battle_entry_view
    from sib.handlers.garden import _render_garden
    from sib.handlers.shop import _shop_view
    from sib.handlers.trading import _pending_view

    # battle_entry_view / _shop_view / _pending_view / _render_garden are async
    # builders; the keyboards are checked via the source-level builders where
    # possible and via direct calls below.
    import asyncio

    async def collect():
        from sqlalchemy import select

        from sib.database.connection import get_session
        from sib.tests.test_parties import create_test_user

        async with get_session() as session:
            user = await create_test_user(session, "ناوبر")
            await session.commit()
            uid = user.id

        _, shop_kb = await _shop_view(uid)
        _, battle_kb = await battle_entry_view(uid)
        _, trade_kb = await _pending_view(uid)
        async with get_session() as session:
            from sib.services.garden import GardenService

            await GardenService.get_or_create(session, uid)
            await session.commit()
        async with get_session() as session:
            _, garden_kb = await _render_garden(uid, session)
        return shop_kb, battle_kb, trade_kb, garden_kb

    shop_kb, battle_kb, trade_kb, garden_kb = asyncio.run(collect())
    for name, kb in (
        ("shop", shop_kb),
        ("battle", battle_kb),
        ("trade", trade_kb),
        ("garden", garden_kb),
    ):
        backs = [b for b in _all_buttons(kb) if "بازگشت به منوی اصلی" in (b.text or "")]
        assert backs, f"{name} panel must have Back to main menu"
        assert all(b.callback_data == "main_menu" for b in backs)


def test_wiki_back_is_context_aware():
    """Wiki entered from cards returns to cards; otherwise to main menu."""
    from sib.handlers.wiki import _wiki_keyboard

    class _C:
        def __init__(self, i, name):
            self.id = i
            self.name_fa = name

    chars = [_C(1, "یک"), _C(2, "دو")]
    kb_from_main = _wiki_keyboard(chars, 0, origin="main_menu")
    backs = [b for b in _all_buttons(kb_from_main) if "بازگشت" in (b.text or "")]
    assert backs and backs[0].callback_data == "main_menu"

    kb_from_cards = _wiki_keyboard(chars, 0, origin="apple_cards")
    backs = [b for b in _all_buttons(kb_from_cards) if "بازگشت" in (b.text or "")]
    assert backs and backs[0].callback_data == "apple_cards"
    assert "کارت‌های من" in backs[0].text


# ---------------------------------------------------------------------------
# Context-aware main menu
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_menu_is_context_aware():
    from sib.handlers.start import handle_main_menu

    def make_cb(chat_type):
        cb = MagicMock()
        cb.id = "cb-main"
        cb.data = "main_menu"
        cb.from_user = MagicMock()
        cb.from_user.id = 4242
        cb.from_user.is_bot = False
        cb.from_user.first_name = "کاربر"
        cb.message = MagicMock()
        cb.message.chat = MagicMock()
        cb.message.chat.type = chat_type
        cb.message.chat.id = -6001 if chat_type == "supergroup" else 4242
        cb.message.message_id = 9
        cb.answer = AsyncMock()
        cb.edit_text = AsyncMock()
        return cb

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    cb = make_cb("supergroup")
    await handle_main_menu(cb, bot)
    kb = bot.edit_message_text.call_args.kwargs.get("reply_markup")
    group_cbs = {b.callback_data for b in _all_buttons(kb)}
    assert "group_games_panel" in group_cbs
    assert "group_rating_stats" in group_cbs

    bot.edit_message_text.reset_mock()
    cb = make_cb("private")
    await handle_main_menu(cb, bot)
    kb = bot.edit_message_text.call_args.kwargs.get("reply_markup")
    private_cbs = {b.callback_data for b in _all_buttons(kb)}
    assert "apple_garden" in private_cbs
    assert "help_menu" in private_cbs


def test_board_refresh_button_exists_in_rematch_keyboard():
    from sib.handlers.games.common import get_rematch_keyboard, with_refresh_button

    kb = with_refresh_button(get_rematch_keyboard(), match_id=42)
    callbacks = [b.callback_data for b in _all_buttons(kb)]
    assert "game_refresh:42" in callbacks
    # Rematch entry point stays non-destructive.
    assert "group_games_panel" in callbacks
