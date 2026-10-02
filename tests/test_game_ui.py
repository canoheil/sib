"""Rendering tests for the refreshed game emoji UI.

Spec (Phase 2):
- Tic Tac Toe:   player 1 = 🍎, player 2 = 🍏, empty = ⬜
- Connect Four:  player 1 = 🍎, player 2 = 🍏, empty = ⬜
- Battleship:    blue/sea visual language for the shot buttons.
"""

import json

import pytest

from sib.database.models import MatchPlayer, User
from sib.games.battleship.engine import BattleshipEngine
from sib.games.connect_four.engine import ConnectFourEngine
from sib.games.tic_tac_toe.engine import TicTacToeEngine


@pytest.fixture
def solo_players():
    u1 = User(id=401, username="ui1", first_name="Player 1")
    u2 = User(id=402, username="ui2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=401, team=1, group_id=-4001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=402, team=2, group_id=-4002)
    mp2.user = u2
    return [mp1, mp2]


def test_tic_tac_toe_uses_apple_icons(solo_players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo")

    # X (player 1) and O (player 2) map to 🍎 and 🍏 in every render.
    state["board"][0] = "X"
    state["board"][4] = "O"

    text = engine.render_for_group(state, -4001, 401)
    assert "🍎" in text
    assert "🍏" in text
    assert "⬜" in text

    kb = engine.get_keyboard(state, 1, 401)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "🍎" in labels
    assert "🍏" in labels
    assert "⬜" in labels
    # Old icons must be gone.
    assert "❌" not in text and "⭕" not in text
    assert "❌" not in labels and "⭕" not in labels


def test_connect_four_uses_apple_icons(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo")

    state["board"][5][0] = "A"
    state["board"][5][1] = "B"

    text = engine.render_for_group(state, -4001, 401)
    assert "🍎" in text
    assert "🍏" in text
    assert "⬜" in text
    assert "🔴" not in text and "🟡" not in text


def test_battleship_uses_glass_and_shot_button_styling(solo_players):
    """Unselected cells look visually minimal; shot cells change the BUTTON ITSELF.

    Design contract (UX audit): the visual state lives in the button
    text - visually minimal · when unselected (blue Telegram button style),
    🌊 when shot at water (miss), 💥 on hit, 🔥 on a sunk ship cell.
    """
    engine = BattleshipEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo", fleet_seed_a=1, fleet_seed_b=2)

    # Force known shots: (0,0) miss and (1,1) hit for team A.
    state["teams"]["A"]["shots_taken"] = {"0,0": "miss", "1,1": "hit"}

    text = engine.render_for_group(state, -4001, 401)
    assert "·" in text   # unselected cells are visually minimal
    assert "🌊" in text  # miss: wave emoji
    assert "💥" in text  # hits
    assert "░" not in text  # old glass character is gone
    assert "🟦" not in text  # old blue square is gone

    # Keyboard layout: header row + 8 data rows (row label + 8 cells)
    kb = engine.get_keyboard(state, 1, 401)
    # Cell (0,0) is at keyboard row 1, col 1 (offset by header + label)
    assert kb.inline_keyboard[1][1].text == "🌊"  # shot water: wave emoji
    assert kb.inline_keyboard[2][2].text == "💥"  # hit
    assert kb.inline_keyboard[3][3].text == "·"   # unselected: minimal
    # Shot cells are noop, unshot cell on my turn is clickable.
    assert kb.inline_keyboard[1][1].callback_data == "noop"
    assert kb.inline_keyboard[2][2].callback_data == "noop"
    assert kb.inline_keyboard[3][3].callback_data.startswith("bs_action:")

    # Sunk-ship cells render as 🔥 on both text and keyboard.
    state["teams"]["B"]["ships"][0]["sunk"] = True
    for cell in state["teams"]["B"]["ships"][0]["cells"]:
        state["teams"]["A"]["shots_taken"][f"{cell[0]},{cell[1]}"] = "hit"
    kb = engine.get_keyboard(state, 1, 401)
    r0, c0 = state["teams"]["B"]["ships"][0]["cells"][0]
    # Offset by header row (+1) and row label column (+1)
    assert kb.inline_keyboard[r0 + 1][c0 + 1].text == "🔥"
