"""Glass-button colors — no inline button renders transparent any more.

Policy (Bot API ``style`` field):
- success (green) -> positive/confirm actions (buy, join, keep, accept)
- danger  (red)   -> back / cancel / destructive actions, dead cells
- primary (blue)  -> navigation / informational actions, playable cells

The only transparent buttons left in the codebase are the documented
legacy-client fallback in ``services.telegram_ui`` (old aiogram / Bot API
builds that reject ``style``); there the color lives inside the label.
"""

import re
from pathlib import Path

import pytest

from sib.database.models import MatchPlayer, User
from sib.games.apple_battle.engine import AppleBattleEngine
from sib.games.battleship.engine import BattleshipEngine
from sib.games.connect_four.engine import ConnectFourEngine
from sib.games.high_card.engine import HighCardEngine
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.games.tic_tac_toe.engine import TicTacToeEngine
from sib.keyboards.common import keyboard_from_specs, styled_button

STYLES = {"primary", "success", "danger"}
REPO_ROOT = Path(__file__).resolve().parent.parent
# The two buttons of the legacy fallback path: no `style` on purpose.
ALLOWED_UNSTYLED_FILES = {"services/telegram_ui.py"}


@pytest.fixture
def solo_players():
    u1 = User(id=901, username="glass1", first_name="Player 1")
    u2 = User(id=902, username="glass2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=901, team=1, group_id=-9001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=902, team=2, group_id=-9002)
    mp2.user = u2
    return [mp1, mp2]


def _buttons(kb):
    return [button for row in kb.inline_keyboard for button in row]


def _by_text(kb):
    return {button.text: button for button in _buttons(kb)}


def _by_callback(kb):
    return {button.callback_data: button for button in _buttons(kb)}


def test_every_engine_board_is_fully_colored(solo_players):
    """A live board never contains a transparent button."""
    boards = []

    ttt = TicTacToeEngine()
    state = ttt.create_initial_state(list(solo_players), mode="solo")
    state["board"][0] = "X"
    state["board"][4] = "O"
    boards.append(ttt.get_keyboard(state, 1, 901))

    c4 = ConnectFourEngine()
    state = c4.create_initial_state(list(solo_players), mode="solo")
    state["board"][5][0] = "A"
    state["board"][0][1] = "B"  # column 2 is full
    boards.append(c4.get_keyboard(state, 1, 901))

    rps = RockPaperScissorsEngine()
    boards.append(rps.get_keyboard(rps.create_initial_state(list(solo_players), mode="solo"), 1, 901))

    hc = HighCardEngine()
    boards.append(hc.get_keyboard(hc.create_initial_state(list(solo_players), mode="solo"), 1, 901))

    bs = BattleshipEngine()
    boards.append(
        bs.get_keyboard(
            bs.create_initial_state(list(solo_players), mode="solo", fleet_seed_a=1, fleet_seed_b=2),
            1,
            901,
        )
    )

    apple = AppleBattleEngine()
    boards.append(apple.get_keyboard({}, 1, 901))

    for board in boards:
        assert board.inline_keyboard, "board rendered empty"
        for button in _buttons(board):
            assert button.style in STYLES, (button.text, button.callback_data, button.style)


def test_tic_tac_toe_cells_carry_their_apple_color(solo_players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo")
    state["board"][0] = "X"  # 🍎 -> red
    state["board"][1] = "O"  # 🍏 -> green
    # X (and therefore the turn) is randomized per match: shoot from X's seat.
    x_id = next(int(pid) for pid, data in state["players"].items() if data["symbol"] == "X")

    kb = engine.get_keyboard(state, 1, x_id)

    assert kb.inline_keyboard[0][0].text == "🍎"
    assert kb.inline_keyboard[0][0].style == "danger"
    assert kb.inline_keyboard[0][0].callback_data == "noop"
    assert kb.inline_keyboard[0][1].text == "🍏"
    assert kb.inline_keyboard[0][1].style == "success"
    # Empty cell on my turn: blue and playable.
    assert kb.inline_keyboard[0][2].text == "⬜"
    assert kb.inline_keyboard[0][2].style == "primary"
    assert kb.inline_keyboard[0][2].callback_data == "ttt_action:1:2"


def test_connect_four_full_column_is_red_and_playable_columns_blue(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo")
    state["board"][0][3] = "A"  # column 4 (index 3) is full
    state["current_player_id"] = 901  # pin the turn instead of trusting the seed

    kb = engine.get_keyboard(state, 1, 901)
    assert kb.inline_keyboard[0][3].text == "🚫"
    assert kb.inline_keyboard[0][3].style == "danger"
    assert kb.inline_keyboard[0][3].callback_data == "noop"
    assert kb.inline_keyboard[0][0].style == "primary"
    assert kb.inline_keyboard[0][0].callback_data == "c4_action:1:0"


def test_high_card_keep_is_green_swap_is_blue_and_card_value_is_hidden(solo_players):
    engine = HighCardEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo")

    # Phase 1: only the draw action.
    kb = engine.get_keyboard(state, 1, 901)
    assert kb.inline_keyboard[0][0].style == "primary"

    # Phase 2: the private card value stays out of the group keyboard.
    player = state["players"]["901"]
    player["card_rank"] = 14
    player["card_suit"] = "♠️"
    kb = engine.get_keyboard(state, 1, 901)
    by_text = _by_text(kb)
    assert by_text["✅ نگه داشتن"].style == "success"
    assert by_text["🔄 تعویض"].style == "primary"
    labels = " ".join(button.text for button in _buttons(kb))
    assert "تک / آس (A)" not in labels and "♠️" not in labels

    # Locked, waiting for the opponent: neutral informational button.
    player["locked"] = True
    kb = engine.get_keyboard(state, 1, 901)
    assert [b.style for b in _buttons(kb)] == ["primary"]
    assert "🔒 تصمیم ثبت شد" == _buttons(kb)[0].text


def test_rock_paper_scissors_choices_and_lock_are_blue(solo_players):
    engine = RockPaperScissorsEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo")

    kb = engine.get_keyboard(state, 1, 901)
    assert {b.style for b in _buttons(kb)} == {"primary"}

    state["players"]["901"]["choice"] = "rock"
    state["players"]["901"]["locked"] = True
    kb = engine.get_keyboard(state, 1, 901)
    assert len(_buttons(kb)) == 1
    assert _buttons(kb)[0].style == "primary"


def test_apple_battle_refresh_button_is_blue():
    kb = AppleBattleEngine().get_keyboard({}, 42, 901)
    button = kb.inline_keyboard[0][0]
    assert button.callback_data == "game_refresh:42"
    assert button.style == "primary"


def test_refresh_row_is_blue_by_default_and_green_for_battleship(solo_players):
    from sib.services.telegram_ui import (
        BATTLESHIP_REFRESH_BUTTON_STYLE,
        REFRESH_BUTTON_STYLE,
        board_refresh_style,
        with_refresh_button,
    )

    assert REFRESH_BUTTON_STYLE == "primary"
    assert BATTLESHIP_REFRESH_BUTTON_STYLE == "success"
    assert board_refresh_style("battleship") == "success"
    assert board_refresh_style("connect_four") == "primary"
    assert board_refresh_style(None) == "primary"

    engine = ConnectFourEngine()
    board = engine.get_keyboard(
        engine.create_initial_state(list(solo_players), mode="solo"), 1, 901
    )
    kb = with_refresh_button(board, 1, refresh_style=board_refresh_style("connect_four"))
    assert kb.inline_keyboard[-1][0].style == "primary"

    bs = BattleshipEngine()
    kb = with_refresh_button(board, 1, refresh_style=board_refresh_style("battleship"))
    assert kb.inline_keyboard[-1][0].style == "success"


def test_join_and_new_game_entries_are_green():
    from sib.services.game_sync import final_keyboard
    from sib.services.invasion_v2.presentation import join_keyboard

    join = _by_callback(join_keyboard(7))["inv2:join:7"]
    assert join.style == "success"

    new_game = _by_callback(final_keyboard())["group_games_panel"]
    assert new_game.style == "success"


def test_duel_invitation_colors():
    from sib.handlers.apple_duel import invitation_keyboard

    by_callback = _by_callback(invitation_keyboard("d" * 32))
    assert by_callback["ad:accept:" + "d" * 32].style == "success"
    assert by_callback["ad:reject:" + "d" * 32].style == "danger"
    assert by_callback["ad:change:" + "d" * 32].style == "primary"


def test_module_panel_builders_fill_every_style():
    """Tuple-based panels declare colors; a spec without a style stays opt-in."""
    kb = keyboard_from_specs([[("a", "one"), ("b", "two", "danger")]], default_style="primary")
    assert kb.inline_keyboard[0][0].style == "primary"
    assert kb.inline_keyboard[0][1].style == "danger"
    assert styled_button(text="x", callback_data="y", style="success").style == "success"


def test_admin_broadcast_controls_are_colored():
    from sib.handlers.admin_broadcast import keyboard

    targets = _by_callback(keyboard())
    assert targets["bcast:target:all"].style == "primary"
    assert targets["bcast:target:one"].style == "primary"
    assert targets["bcast:cancel"].style == "danger"

    preview = _by_callback(keyboard(preview=True))
    assert preview["bcast:send"].style == "success"
    assert preview["bcast:edit"].style == "primary"
    assert preview["bcast:cancel"].style == "danger"


def test_invasion_console_on_green_off_red_rest_blue():
    from sib.handlers.inv2_dashboard import keyboard

    by_callback = _by_callback(keyboard(1))
    assert by_callback["inv2adm:1:on"].style == "success"
    assert by_callback["inv2adm:1:off"].style == "danger"
    assert by_callback["inv2adm:1:refresh"].style == "primary"
    for callback, button in by_callback.items():
        assert button.style in STYLES, callback


def test_admin_user_panel_defaults_to_blue_and_marks_confirm_and_cancel():
    from sib.handlers.admin_users import kb

    panel = kb([[("🍏 سیب‌ها", "uadm:menu:apples:5")],
                [("✅ تأیید", "uadm:confirm:t", "success"), ("❌ لغو", "uadm:back:5", "danger")]])
    by_callback = _by_callback(panel)
    assert by_callback["uadm:menu:apples:5"].style == "primary"  # default, never transparent
    assert by_callback["uadm:confirm:t"].style == "success"
    assert by_callback["uadm:back:5"].style == "danger"


def test_production_code_has_no_unstyled_buttons_outside_the_legacy_fallback():
    """Regression guard: a new raw ``InlineKeyboardButton`` must pick a color.

    ``services.telegram_ui`` is the ONE documented exception (its fallback
    buttons carry the color in the label because old clients reject ``style``).
    """
    pattern = re.compile(r"InlineKeyboardButton\(")
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & {".venv", ".git", "tests", "node_modules", "__pycache__", "tmp"}:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(source):
            index = match.end()
            depth = 1
            while index < len(source) and depth:
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                index += 1
            call = source[match.start():index]
            if "style=" in call:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in ALLOWED_UNSTYLED_FILES:
                continue
            offenders.append(f"{relative}:{source[:match.start()].count(chr(10)) + 1}")

    assert offenders == [], (
        "Unstyled inline buttons found - use primary_button/success_button/"
        "danger_button or keyboard_from_specs: " + ", ".join(offenders)
    )


def test_legacy_work_button_fallback_stays_transparent():
    """The old-client path is the ONE place a button stays uncolored."""
    from sib.services import telegram_ui

    legacy = telegram_ui.work_button_kb(supports_style=False).inline_keyboard[0][0]
    assert legacy.style is None
    assert legacy.text.startswith("🟢 ")
