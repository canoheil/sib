"""Tests for Battleship game engine and rules."""

import pytest
from sib.database.models import User, MatchPlayer
from sib.games.battleship.engine import BattleshipEngine


@pytest.fixture
def solo_players():
    u1 = User(id=401, username="p1", first_name="Player 1")
    u2 = User(id=402, username="p2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=401, team="A", group_id=-4001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=402, team="B", group_id=-4002)
    mp2.user = u2
    return [mp1, mp2]


def test_fleet_generation():
    engine = BattleshipEngine()
    fleet = engine.generate_fleet(custom_seed=42)

    assert len(fleet) == 5
    sizes = sorted([s["size"] for s in fleet])
    assert sizes == [2, 2, 3, 3, 4]

    # Verify no overlaps and all coordinates within 8x8
    all_cells = []
    for s in fleet:
        assert len(s["cells"]) == s["size"]
        for r, c in s["cells"]:
            assert 0 <= r < 8
            assert 0 <= c < 8
            all_cells.append((r, c))

    assert len(all_cells) == 14
    assert len(set(all_cells)) == 14


def test_battleship_initial_state(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    assert state["game"] == "battleship"
    assert state["teams"]["A"]["remaining_cells"] == 14
    assert state["teams"]["B"]["remaining_cells"] == 14
    assert state["current_player_id"] == 401
    assert state["turn_order"] == [401, 402]


def test_battleship_validation(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Inactive player cannot shoot
    is_valid, reason = engine.validate_action(state, 402, {"r": 0, "c": 0})
    assert not is_valid
    assert "هنوز نوبت شما نشده است" in reason

    # Out of bounds
    is_valid, reason = engine.validate_action(state, 401, {"r": 8, "c": 0})
    assert not is_valid
    assert "نامعتبر" in reason

    # Valid shot
    is_valid, reason = engine.validate_action(state, 401, {"r": 0, "c": 0})
    assert is_valid
    state = engine.apply_action(state, 401, {"r": 0, "c": 0})

    # Cannot shoot at same cell again
    # Switch back turn to 401
    state["current_player_id"] = 401
    is_valid, reason = engine.validate_action(state, 401, {"r": 0, "c": 0})
    assert not is_valid
    assert "قبلاً شلیک کرده‌اید" in reason


def test_battleship_hit_miss_and_sinking(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Let's inspect team B's ships to shoot accurately
    b_fleet = state["teams"]["B"]["ships"]
    first_ship = b_fleet[0]
    ship_cells = first_ship["cells"]

    # Team A shoots at first cell of first_ship -> HIT
    r0, c0 = ship_cells[0]
    state = engine.apply_action(state, 401, {"r": r0, "c": c0})

    assert state["teams"]["B"]["remaining_cells"] == 13
    assert state["teams"]["A"]["shots_taken"][f"{r0},{c0}"] == "hit"
    assert "اصابت کرد" in state["last_event"] or "غرق شد" in state["last_event"]
    assert state["current_player_id"] == 402  # Turn switches to Team B

    # Team B fires at an empty cell on Team A's grid -> MISS
    # Let's find an empty cell on Team A's grid
    a_ship_cells = {tuple(c) for s in state["teams"]["A"]["ships"] for c in s["cells"]}
    empty_cell = next((r, c) for r in range(8) for c in range(8) if (r, c) not in a_ship_cells)
    state = engine.apply_action(state, 402, {"r": empty_cell[0], "c": empty_cell[1]})

    assert state["teams"]["A"]["remaining_cells"] == 14
    assert state["teams"]["B"]["shots_taken"][f"{empty_cell[0]},{empty_cell[1]}"] == "miss"
    assert "به آب خورد" in state["last_event"]
    assert state["current_player_id"] == 401  # Back to Team A


def test_battleship_win_condition(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    # Team A destroys all 14 cells of Team B
    b_cells = [cell for ship in state["teams"]["B"]["ships"] for cell in ship["cells"]]

    for r, c in b_cells:
        state["current_player_id"] = 401
        state = engine.apply_action(state, 401, {"r": r, "c": c})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == "A"
    assert "ناوگان دشمن را غرق کرد" in result.summary


def test_battleship_rendering_and_keyboard(solo_players):
    engine = BattleshipEngine()
    state = engine.create_initial_state(solo_players, mode="solo", fleet_seed_a=10, fleet_seed_b=20)

    text = engine.render_for_group(state, -4001, 401)
    assert "جنگ کشتی‌ها" in text
    assert "رادار شلیک‌های شما" in text
    assert "نوبت شماست" in text
    # Coordinate grid must be present in the rendered text.
    assert "A B C D E F G H" in text

    kb = engine.get_keyboard(state, 1, 401)
    # 1 header row + 8 data rows = 9 total
    assert len(kb.inline_keyboard) == 9
    # Header row: corner label + 8 column letters
    assert len(kb.inline_keyboard[0]) == 9
    assert kb.inline_keyboard[0][1].text == "A"
    assert kb.inline_keyboard[0][8].text == "H"
    # Data rows: row label + 8 cells
    assert kb.inline_keyboard[1][0].text == "1"
    assert kb.inline_keyboard[8][0].text == "8"
    # Cell at (0,0) is at keyboard row 1, col 1 (offset by header + label)
    assert kb.inline_keyboard[1][1].callback_data.startswith("bs_action:1:0:0")
