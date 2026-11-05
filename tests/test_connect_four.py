"""Tests for Connect Four game engine and rules."""

import pytest
from sib.database.models import User, MatchPlayer
from sib.games.connect_four.engine import ConnectFourEngine


@pytest.fixture
def solo_players():
    u1 = User(id=301, username="p1", first_name="Player 1")
    u2 = User(id=302, username="p2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=301, team="A", group_id=-3001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=302, team="B", group_id=-3002)
    mp2.user = u2
    return [mp1, mp2]


@pytest.fixture
def team_players_2v2():
    users = [User(id=310 + i, username=f"p{i}", first_name=f"Player {i}") for i in range(4)]
    players = [
        MatchPlayer(match_id=2, user_id=310, team="A", group_id=-3001),
        MatchPlayer(match_id=2, user_id=311, team="A", group_id=-3001),
        MatchPlayer(match_id=2, user_id=312, team="B", group_id=-3002),
        MatchPlayer(match_id=2, user_id=313, team="B", group_id=-3002),
    ]
    for p, u in zip(players, users):
        p.user = u
    return players


def test_c4_initial_state_solo(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    assert state["game"] == "connect_four"
    assert len(state["board"]) == 6
    assert len(state["board"][0]) == 7
    assert state["current_player_id"] == 301
    assert state["turn_order"] == [301, 302]


def test_c4_team_turn_rotation_2v2(team_players_2v2):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(team_players_2v2, mode="2v2")

    # Turn order should interleave A and B: [310, 312, 311, 313]
    assert state["turn_order"] == [310, 312, 311, 313]
    assert state["current_player_id"] == 310

    # Turn 0 (Team A - 310)
    state = engine.apply_action(state, 310, {"col": 0})
    assert state["current_player_id"] == 312

    # Turn 1 (Team B - 312)
    state = engine.apply_action(state, 312, {"col": 1})
    assert state["current_player_id"] == 311

    # Turn 2 (Team A - 311)
    state = engine.apply_action(state, 311, {"col": 0})
    assert state["current_player_id"] == 313

    # Turn 3 (Team B - 313)
    state = engine.apply_action(state, 313, {"col": 1})
    assert state["current_player_id"] == 310  # Loops back to 310!


def test_c4_gravity_and_column_full(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Drop 6 pieces into column 0
    for i in range(6):
        pid = state["current_player_id"]
        is_valid, _ = engine.validate_action(state, pid, {"col": 0})
        assert is_valid
        state = engine.apply_action(state, pid, {"col": 0})

    # Lowest row (5) should be filled first, highest row (0) last
    board = state["board"]
    assert board[5][0] == "A"
    assert board[4][0] == "B"
    assert board[0][0] is not None

    # 7th piece in column 0 must be rejected
    curr_pid = state["current_player_id"]
    is_valid, reason = engine.validate_action(state, curr_pid, {"col": 0})
    assert not is_valid
    assert "این ستون پر شده است" in reason


def test_c4_horizontal_win(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Team A plays cols 0, 1, 2, 3 on bottom row
    # Team B plays col 6
    moves = [
        (301, 0), (302, 6),
        (301, 1), (302, 6),
        (301, 2), (302, 6),
        (301, 3), # 4 in a row!
    ]
    for pid, col in moves:
        state = engine.apply_action(state, pid, {"col": col})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == "A"
    assert "(A)" in result.summary


def test_c4_vertical_win(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Team A stacks 4 in col 2
    # Team B stacks in col 3
    moves = [
        (301, 2), (302, 3),
        (301, 2), (302, 3),
        (301, 2), (302, 3),
        (301, 2), # 4th vertical piece
    ]
    for pid, col in moves:
        state = engine.apply_action(state, pid, {"col": col})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert result.winner_team == "A"


def test_c4_diagonal_win(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Build diagonal \
    # Col 0: A
    # Col 1: B, A
    # Col 2: B, B, A
    # Col 3: B, B, B, A
    moves = [
        (301, 0),  # A row 5 col 0
        (302, 1),  # B row 5 col 1
        (301, 1),  # A row 4 col 1
        (302, 2),  # B row 5 col 2
        (301, 3),  # A (temporary move to burn turn)
        (302, 2),  # B row 4 col 2
        (301, 2),  # A row 3 col 2
        (302, 3),  # B row 4 col 3
        (301, 4),  # A (temporary)
        (302, 3),  # B row 3 col 3
        (301, 3),  # A row 2 col 3 -> completes diagonal (5,0), (4,1), (3,2), (2,3)
    ]
    for pid, col in moves:
        if not engine.is_finished(state):
            state = engine.apply_action(state, pid, {"col": col})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert result.winner_team == "A"


def test_c4_rendering_and_keyboard(solo_players):
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    text = engine.render_for_group(state, -3001, 301)
    assert "چهار در یک خط" in text
    assert "🍎" in text
    assert "🍏" in text
    assert "نوبت شماست" in text

    kb = engine.get_keyboard(state, 1, 301)
    assert len(kb.inline_keyboard[0]) == 7
    assert kb.inline_keyboard[0][0].callback_data == "c4_action:1:0"

    # Opponent keyboard has noop
    kb_opp = engine.get_keyboard(state, 1, 302)
    assert kb_opp.inline_keyboard[0][0].callback_data == "noop"


# ---------------------------------------------------------------------------
# Turn Clarity Tests
# ---------------------------------------------------------------------------


def test_c4_turn_clarity_solo_initial(solo_players):
    """Verify solo mode shows correct turn indicator at game start."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Player 301 (team A, 🍎) goes first
    text = engine.render_for_group(state, -3001, 301)
    assert "🍎 **نوبت شماست!**" in text

    # Player 302 (team B, 🍏) sees waiting
    text_opp = engine.render_for_group(state, -3002, 302)
    assert "⏳ نوبت حریف (🍎 \u200ePlayer 1\u200e)" in text_opp


def test_c4_turn_clarity_solo_after_move(solo_players):
    """Verify solo mode turn indicator updates after a valid move."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Player 301 makes a move
    state = engine.apply_action(state, 301, {"col": 3})

    # Now player 302 should see "your turn"
    text = engine.render_for_group(state, -3002, 302)
    assert "🍏 **نوبت شماست!**" in text

    # Player 301 sees waiting for opponent
    text_opp = engine.render_for_group(state, -3001, 301)
    assert "⏳ نوبت حریف (🍏 \u200ePlayer 2\u200e)" in text_opp


def test_c4_turn_clarity_solo_invalid_move(solo_players):
    """Verify invalid moves do not change the turn indicator."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Player 302 tries to move (invalid - not their turn)
    is_valid, _ = engine.validate_action(state, 302, {"col": 0})
    assert not is_valid

    # Turn should still be 301
    assert state["current_player_id"] == 301
    text = engine.render_for_group(state, -3001, 301)
    assert "نوبت شماست" in text


def test_c4_turn_clarity_solo_game_over(solo_players):
    """Verify no turn indicator is shown when game is finished."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(solo_players, mode="solo")

    # Player 301 wins horizontally
    moves = [
        (301, 0), (302, 6),
        (301, 1), (302, 6),
        (301, 2), (302, 6),
        (301, 3),  # 4 in a row!
    ]
    for pid, col in moves:
        state = engine.apply_action(state, pid, {"col": col})

    assert engine.is_finished(state)

    # No turn indicator should be present
    text = engine.render_for_group(state, -3001, 301)
    assert "نوبت شماست" not in text
    assert "نوبت حریف" not in text
    assert "پایان بازی" in text


def test_c4_turn_clarity_team_your_turn(team_players_2v2):
    """Verify team mode shows correct turn indicator when you are the active player.

    In shared group mode (both teammates in same group), the current player
    sees their name instead of "your turn" to avoid confusion.
    """
    engine = ConnectFourEngine()
    state = engine.create_initial_state(team_players_2v2, mode="2v2")

    # Turn order: [310, 312, 311, 313]
    # Player 310 (team A, "Player 0") goes first
    # Since both team A players are in group -3001 (shared group),
    # player 310 sees "نوبت Player 0 است!" instead of "نوبت شماست!"
    text = engine.render_for_group(state, -3001, 310)
    assert "🍎 **نوبت \u200ePlayer 0\u200e است!**" in text


def test_c4_turn_clarity_team_teammate_turn(team_players_2v2):
    """Verify team mode shows 'your team's turn: TeammateName' when teammate is active."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(team_players_2v2, mode="2v2")

    # Turn order: [310, 312, 311, 313]
    # Player names: 310="Player 0", 311="Player 1", 312="Player 2", 313="Player 3"

    # After 310 moves, it's 312's turn (team B)
    state = engine.apply_action(state, 310, {"col": 0})

    # Player 311 (team A) sees opponent's turn (312 is team B)
    text = engine.render_for_group(state, -3001, 311)
    assert "⏳ نوبت تیم حریف: 👤 \u200ePlayer 2\u200e (🍏)" in text

    # After 312 moves, it's 311's turn (team A)
    state = engine.apply_action(state, 312, {"col": 1})

    # Player 311 (team A, "Player 1") sees their turn
    # Since group is shared, they see "نوبت Player 1 است!"
    text = engine.render_for_group(state, -3001, 311)
    assert "🍎 **نوبت \u200ePlayer 1\u200e است!**" in text

    # Player 310 (team A, same team as 311) sees "your team's turn: Player 1"
    # (311's name is "Player 1")
    text_teammate = engine.render_for_group(state, -3001, 310)
    assert "🍎 **نوبت تیم شما:** 👤 \u200ePlayer 1\u200e" in text_teammate


def test_c4_turn_clarity_team_opponent_turn(team_players_2v2):
    """Verify team mode shows 'opponent team's turn' when opponent is active."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(team_players_2v2, mode="2v2")

    # Turn order: [310, 312, 311, 313]
    # Player 310 (team A) goes first

    # Player 312 (team B) sees opponent's turn
    text = engine.render_for_group(state, -3002, 312)
    assert "⏳ نوبت تیم حریف: 👤 \u200ePlayer 0\u200e (🍎)" in text

    # Player 313 (team B) also sees opponent's turn
    text2 = engine.render_for_group(state, -3002, 313)
    assert "⏳ نوبت تیم حریف: 👤 \u200ePlayer 0\u200e (🍎)" in text2


def test_c4_turn_clarity_team_full_rotation(team_players_2v2):
    """Verify turn clarity through a full rotation in 2v2 mode."""
    engine = ConnectFourEngine()
    state = engine.create_initial_state(team_players_2v2, mode="2v2")

    # Turn order: [310, 312, 311, 313]
    # Player names: 310="Player 0", 311="Player 1", 312="Player 2", 313="Player 3"

    # Turn 0: 310 (team A) active
    # Shared group: 310 sees "نوبت Player 0 است!"
    text_310 = engine.render_for_group(state, -3001, 310)
    assert "🍎 **نوبت \u200ePlayer 0\u200e است!**" in text_310

    text_311 = engine.render_for_group(state, -3001, 311)
    assert "🍎 **نوبت تیم شما:** 👤 \u200ePlayer 0\u200e" in text_311

    text_312 = engine.render_for_group(state, -3002, 312)
    assert "⏳ نوبت تیم حریف: 👤 \u200ePlayer 0\u200e (🍎)" in text_312

    state = engine.apply_action(state, 310, {"col": 0})

    # Turn 1: 312 (team B) active
    # Shared group: 312 sees "نوبت Player 2 است!"
    text_312 = engine.render_for_group(state, -3002, 312)
    assert "🍏 **نوبت \u200ePlayer 2\u200e است!**" in text_312

    text_313 = engine.render_for_group(state, -3002, 313)
    assert "🍏 **نوبت تیم شما:** 👤 \u200ePlayer 2\u200e" in text_313

    text_310 = engine.render_for_group(state, -3001, 310)
    assert "⏳ نوبت تیم حریف: 👤 \u200ePlayer 2\u200e (🍏)" in text_310

    state = engine.apply_action(state, 312, {"col": 1})

    # Turn 2: 311 (team A, "Player 1") active
    # Shared group: 311 sees "نوبت Player 1 است!"
    text_311 = engine.render_for_group(state, -3001, 311)
    assert "🍎 **نوبت \u200ePlayer 1\u200e است!**" in text_311

    text_310 = engine.render_for_group(state, -3001, 310)
    assert "🍎 **نوبت تیم شما:** 👤 \u200ePlayer 1\u200e" in text_310

    state = engine.apply_action(state, 311, {"col": 2})

    # Turn 3: 313 (team B, "Player 3") active
    # Shared group: 313 sees "نوبت Player 3 است!"
    text_313 = engine.render_for_group(state, -3002, 313)
    assert "🍏 **نوبت \u200ePlayer 3\u200e است!**" in text_313

    text_312 = engine.render_for_group(state, -3002, 312)
    assert "🍏 **نوبت تیم شما:** 👤 \u200ePlayer 3\u200e" in text_312
