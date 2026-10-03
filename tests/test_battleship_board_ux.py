"""Focused tests for Battleship board UX improvements.

Covers:
- Empty untouched cells (visually minimal · instead of ░)
- Telegram button styles for the corner, headers, cells, and refresh row
- 🌊 for miss, 💥 for hit, 🔥 for sunk
- Persistent hit/miss rendering
- Correct row/column coordinates
- Correct callback coordinates after adding labels
- Hidden enemy ships remain hidden
- Last shot textual confirmation
"""

import pytest
from sib.database.models import User, MatchPlayer
from sib.games.battleship.engine import BattleshipEngine
from sib.services.telegram_ui import with_refresh_button


@pytest.fixture
def solo_players():
    u1 = User(id=401, username="ux1", first_name="Player 1")
    u2 = User(id=402, username="ux2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=401, team="A", group_id=-4001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=402, team="B", group_id=-4002)
    mp2.user = u2
    return [mp1, mp2]


# ---------------------------------------------------------------------------
# Empty untouched cells
# ---------------------------------------------------------------------------


def test_untouched_cells_use_minimal_dot(solo_players):
    """Untouched cells must use the minimal · character, not ░ or 🟦."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    text = engine.render_for_group(state, -4001, 401)
    assert "·" in text
    assert "░" not in text
    assert "🟦" not in text

    kb = engine.get_keyboard(state, 1, 401)
    # Check an untouched cell (row 1, col 1 in keyboard = cell 0,0)
    cell = kb.inline_keyboard[1][1]
    assert cell.text == "·"


def test_untouched_cells_are_blue_primary_buttons(solo_players):
    """Untouched cells explicitly use the Telegram primary (blue) style."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    kb = engine.get_keyboard(state, 1, 401)
    for row_idx in range(1, 9):  # skip header row
        for col_idx in range(1, 9):  # skip row label
            btn = kb.inline_keyboard[row_idx][col_idx]
            assert btn.style == "primary"
            assert btn.text == "·"


# ---------------------------------------------------------------------------
# Miss / Hit rendering
# ---------------------------------------------------------------------------


def test_miss_shows_wave_emoji(solo_players):
    """A missed shot must display 🌊 on both text and keyboard."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Force a miss at (0,0)
    state["teams"]["A"]["shots_taken"]["0,0"] = "miss"

    text = engine.render_for_group(state, -4001, 401)
    assert "🌊" in text

    kb = engine.get_keyboard(state, 1, 401)
    # Cell (0,0) is at keyboard row 1, col 1
    assert kb.inline_keyboard[1][1].text == "🌊"
    assert kb.inline_keyboard[1][1].callback_data == "noop"
    assert kb.inline_keyboard[1][1].style == "primary"


def test_hit_shows_explosion_emoji(solo_players):
    """A successful hit must display 💥 on both text and keyboard."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Force a hit at (0,0)
    state["teams"]["A"]["shots_taken"]["0,0"] = "hit"

    text = engine.render_for_group(state, -4001, 401)
    assert "💥" in text

    kb = engine.get_keyboard(state, 1, 401)
    assert kb.inline_keyboard[1][1].text == "💥"
    assert kb.inline_keyboard[1][1].callback_data == "noop"
    assert kb.inline_keyboard[1][1].style == "primary"


def test_sunk_ship_shows_fire_emoji(solo_players):
    """A sunk ship cell must display 🔥 on the keyboard."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Mark first ship as sunk and all its cells as hit
    ship = state["teams"]["B"]["ships"][0]
    ship["sunk"] = True
    for cell in ship["cells"]:
        state["teams"]["A"]["shots_taken"][f"{cell[0]},{cell[1]}"] = "hit"

    kb = engine.get_keyboard(state, 1, 401)
    r0, c0 = ship["cells"][0]
    assert kb.inline_keyboard[r0 + 1][c0 + 1].text == "🔥"
    assert kb.inline_keyboard[r0 + 1][c0 + 1].style == "primary"


# ---------------------------------------------------------------------------
# Persistent hit/miss rendering
# ---------------------------------------------------------------------------


def test_hit_miss_persist_across_renders(solo_players):
    """Hit and miss states must persist across multiple renders."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Record shots
    state["teams"]["A"]["shots_taken"]["0,0"] = "miss"
    state["teams"]["A"]["shots_taken"]["1,1"] = "hit"

    # Render multiple times
    for _ in range(3):
        kb = engine.get_keyboard(state, 1, 401)
        assert kb.inline_keyboard[1][1].text == "🌊"  # miss at (0,0)
        assert kb.inline_keyboard[2][2].text == "💥"  # hit at (1,1)
        for row_idx in range(1, 9):
            for col_idx in range(1, 9):
                assert kb.inline_keyboard[row_idx][col_idx].style == "primary"

        text = engine.render_for_group(state, -4001, 401)
        assert "🌊" in text
        assert "💥" in text


# ---------------------------------------------------------------------------
# Row/column coordinates
# ---------------------------------------------------------------------------


def test_keyboard_has_column_header_row(solo_players):
    """The keyboard must have a header row with column letters A-H."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    kb = engine.get_keyboard(state, 1, 401)
    assert len(kb.inline_keyboard) == 9  # header + 8 data rows
    assert [len(row) for row in kb.inline_keyboard] == [9] * 9

    header = kb.inline_keyboard[0]
    assert len(header) == 9  # corner label + 8 columns
    assert header[0].text == " "  # corner label
    assert header[0].callback_data == "noop"
    assert header[0].style == "danger"
    for i, letter in enumerate("ABCDEFGH"):
        assert header[i + 1].text == letter
        assert header[i + 1].callback_data == "noop"
        assert header[i + 1].style == "success"


def test_keyboard_has_row_labels(solo_players):
    """Each data row must start with a row number label 1-8."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    kb = engine.get_keyboard(state, 1, 401)
    for row_idx in range(1, 9):
        label_btn = kb.inline_keyboard[row_idx][0]
        assert label_btn.text == str(row_idx)
        assert label_btn.callback_data == "noop"
        assert label_btn.style == "success"


def test_refresh_button_is_green_without_changing_the_board(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)
    board = engine.get_keyboard(state, 1, 401)
    board_signature = [
        [(button.text, button.callback_data, button.style) for button in row]
        for row in board.inline_keyboard
    ]

    keyboard = with_refresh_button(board, 1)
    keyboard = with_refresh_button(keyboard, 1, refresh_style="success")
    keyboard = with_refresh_button(keyboard, 1, refresh_style="success")

    assert len(keyboard.inline_keyboard) == 10
    assert [len(row) for row in keyboard.inline_keyboard[:-1]] == [9] * 9
    assert [
        [(button.text, button.callback_data, button.style) for button in row]
        for row in keyboard.inline_keyboard[:-1]
    ] == board_signature
    refresh = keyboard.inline_keyboard[-1][0]
    assert refresh.text == "🔄 بروزرسانی صفحه"
    assert refresh.callback_data == "game_refresh:1"
    assert refresh.style == "success"


def test_text_render_has_coordinate_grid(solo_players):
    """The text render must include coordinate labels."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    text = engine.render_for_group(state, -4001, 401)
    # Column header
    assert "A B C D E F G H" in text
    # Row numbers 1-8 in the grid
    for r in range(1, 9):
        assert f"{r} " in text


def test_coord_label_helper():
    """The coordinate label helper must produce correct labels."""
    assert BattleshipEngine._coord_label(0, 0) == "A1"
    assert BattleshipEngine._coord_label(0, 2) == "C1"
    assert BattleshipEngine._coord_label(3, 2) == "C4"
    assert BattleshipEngine._coord_label(7, 7) == "H8"


# ---------------------------------------------------------------------------
# Correct callback coordinates after adding labels
# ---------------------------------------------------------------------------


def test_callback_data_preserves_grid_coordinates(solo_players):
    """Callback data must correctly encode (row, col) despite label offsets."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    kb = engine.get_keyboard(state, 1, 401)

    # Check several cells across the grid
    for r in range(8):
        for c in range(8):
            btn = kb.inline_keyboard[r + 1][c + 1]  # offset by header + label
            expected_cb = f"bs_action:1:{r}:{c}"
            assert btn.callback_data == expected_cb, (
                f"Cell ({r},{c}) should have callback '{expected_cb}' "
                f"but got '{btn.callback_data}'"
            )


def test_shot_cells_have_noop_callback(solo_players):
    """Shot cells (hit/miss/sunk) must have noop callback."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    state["teams"]["A"]["shots_taken"]["0,0"] = "miss"
    state["teams"]["A"]["shots_taken"]["3,5"] = "hit"

    kb = engine.get_keyboard(state, 1, 401)
    assert kb.inline_keyboard[1][1].callback_data == "noop"
    assert kb.inline_keyboard[4][6].callback_data == "noop"


def test_inactive_player_cells_are_noop(solo_players):
    """When it's not the player's turn, all cells should be noop."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Player 402 is not the active player (401 starts)
    kb = engine.get_keyboard(state, 1, 402)
    for r in range(1, 9):
        for c in range(1, 9):
            assert kb.inline_keyboard[r][c].callback_data == "noop"


# ---------------------------------------------------------------------------
# Hidden enemy ships remain hidden
# ---------------------------------------------------------------------------


def test_enemy_ship_positions_not_revealed_in_text(solo_players):
    """Enemy ship positions must not be visible in the rendered text."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    text = engine.render_for_group(state, -4001, 401)

    # The rendered text should only show shots taken, not ship positions.
    # With no shots taken, the grid should be all · (empty).
    b_ships = state["teams"]["B"]["ships"]
    for ship in b_ships:
        for cell in ship["cells"]:
            coord = f"{cell[0]},{cell[1]}"
            # No shot at this cell -> should show as · in text
            if coord not in state["teams"]["A"]["shots_taken"]:
                # The cell should be empty (·) not revealing the ship
                pass  # Grid is all · by default when no shots taken

    # Verify no ship-specific markers exist in the text
    # (no 🔵, 🚢, or any marker that would reveal ship positions)
    lines = text.split("\n")
    grid_lines = [l for l in lines if l.strip().startswith(("1 ", "2 ", "3 ", "4 ", "5 ", "6 ", "7 ", "8 "))]
    for line in grid_lines:
        # Only · should appear (no ship markers)
        cells = line.split()[1:]  # skip row number
        for cell in cells:
            assert cell == "·", f"Unexpected cell marker '{cell}' in grid - ships must be hidden"


def test_enemy_ship_positions_not_revealed_in_keyboard(solo_players):
    """Enemy ship positions must not be visible in the keyboard."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    kb = engine.get_keyboard(state, 1, 401)

    # Every cell without a shot should show · regardless of ship position
    b_ships = state["teams"]["B"]["ships"]
    ship_cells = set()
    for ship in b_ships:
        for cell in ship["cells"]:
            ship_cells.add((cell[0], cell[1]))

    for r in range(8):
        for c in range(8):
            btn = kb.inline_keyboard[r + 1][c + 1]
            coord_key = f"{r},{c}"
            if coord_key not in state["teams"]["A"]["shots_taken"]:
                # Must show empty · even if there's a ship underneath
                assert btn.text == "·", (
                    f"Cell ({r},{c}) reveals ship position! "
                    f"Text: '{btn.text}', is_ship: {(r, c) in ship_cells}"
                )


# ---------------------------------------------------------------------------
# Last shot textual confirmation
# ---------------------------------------------------------------------------


def test_last_shot_hit_confirmation(solo_players):
    """After a hit, the render must show the shot coordinate and 💥 برخورد!"""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Simulate a hit at (2, 2) = "C3"
    state["last_shot"] = {"r": 2, "c": 2, "result": "hit"}

    text = engine.render_for_group(state, -4001, 401)
    assert "🎯 C3" in text
    assert "💥 برخورد!" in text


def test_last_shot_miss_confirmation(solo_players):
    """After a miss, the render must show the shot coordinate and 🌊 خطا!"""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Simulate a miss at (0, 2) = "C1"
    state["last_shot"] = {"r": 0, "c": 2, "result": "miss"}

    text = engine.render_for_group(state, -4001, 401)
    assert "🎯 C1" in text
    assert "🌊 خطا!" in text


def test_no_last_shot_shows_no_confirmation(solo_players):
    """Before any shot, no last-shot confirmation line should appear."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    text = engine.render_for_group(state, -4001, 401)
    assert "🎯" not in text or "🎯 **رادار" in text  # only the radar header uses 🎯
    assert "برخورد!" not in text
    assert "خطا!" not in text


def test_last_shot_set_by_apply_action(solo_players):
    """apply_action must set last_shot in the game state."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Find an empty cell to shoot at (guaranteed miss)
    a_ship_cells = {tuple(c) for s in state["teams"]["A"]["ships"] for c in s["cells"]}
    empty_cell = next((r, c) for r in range(8) for c in range(8) if (r, c) not in a_ship_cells)

    state = engine.apply_action(state, 401, {"r": empty_cell[0], "c": empty_cell[1]})
    assert state["last_shot"] is not None
    assert state["last_shot"]["r"] == empty_cell[0]
    assert state["last_shot"]["c"] == empty_cell[1]
    assert state["last_shot"]["result"] == "miss"


def test_last_shot_hit_set_by_apply_action(solo_players):
    """apply_action must set last_shot result to 'hit' when hitting a ship."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Shoot at first cell of first enemy ship (guaranteed hit)
    b_ship = state["teams"]["B"]["ships"][0]
    r, c = b_ship["cells"][0]

    state = engine.apply_action(state, 401, {"r": r, "c": c})
    assert state["last_shot"]["result"] == "hit"
    assert state["last_shot"]["r"] == r
    assert state["last_shot"]["c"] == c


# ---------------------------------------------------------------------------
# Existing Battleship rules still pass (regression)
# ---------------------------------------------------------------------------


def test_hit_does_not_grant_extra_turn(solo_players):
    """A hit must NOT grant another turn (turn switches to opponent)."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    b_ship = state["teams"]["B"]["ships"][0]
    r, c = b_ship["cells"][0]

    state = engine.apply_action(state, 401, {"r": r, "c": c})
    # Turn must switch to player 402 even after a hit
    assert state["current_player_id"] == 402


def test_cannot_shoot_same_cell_twice(solo_players):
    """A player cannot shoot at the same cell twice."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    state = engine.apply_action(state, 401, {"r": 0, "c": 0})
    state["current_player_id"] = 401  # force turn back
    is_valid, reason = engine.validate_action(state, 401, {"r": 0, "c": 0})
    assert not is_valid
    assert "قبلاً شلیک کرده‌اید" in reason


def test_win_condition_unchanged(solo_players):
    """Win condition: destroy all 14 cells of the enemy fleet."""
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    b_cells = [cell for ship in state["teams"]["B"]["ships"] for cell in ship["cells"]]
    for r, c in b_cells:
        state["current_player_id"] = 401
        state = engine.apply_action(state, 401, {"r": r, "c": c})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert result.winner_team == "A"
