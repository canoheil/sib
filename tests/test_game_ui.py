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


def test_battleship_uses_blue_sea_styling(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(list(solo_players), mode="solo", fleet_seed_a=1, fleet_seed_b=2)

    # Force known shots: (0,0) miss and (1,1) hit for team A.
    state["teams"]["A"]["shots_taken"] = {"0,0": "miss", "1,1": "hit"}

    text = engine.render_for_group(state, -4001, 401)
    assert "🟦" in text  # unshot water cells use the blue sea palette
    assert "🌊" in text  # misses
    assert "💥" in text  # hits

    kb = engine.get_keyboard(state, 1, 401)
    assert kb.inline_keyboard[0][0].text == "🌊"
    assert kb.inline_keyboard[1][1].text == "💥"
    assert kb.inline_keyboard[2][2].text == "🟦"
    # Unshot cell on my turn is clickable, shot cells are noop.
    assert kb.inline_keyboard[0][0].callback_data == "noop"
    assert kb.inline_keyboard[2][2].callback_data.startswith("bs_action:")
