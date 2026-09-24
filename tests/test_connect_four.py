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
    assert "قرمز (A)" in result.summary


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
    assert "🔴" in text
    assert "نوبت شماست" in text

    kb = engine.get_keyboard(state, 1, 301)
    assert len(kb.inline_keyboard[0]) == 7
    assert kb.inline_keyboard[0][0].callback_data == "c4_action:1:0"

    # Opponent keyboard has noop
    kb_opp = engine.get_keyboard(state, 1, 302)
    assert kb_opp.inline_keyboard[0][0].callback_data == "noop"
