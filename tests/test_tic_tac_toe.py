"""Tests for Tic-Tac-Toe game engine and rules."""

import pytest
from sib.database.models import User, MatchPlayer
from sib.games.tic_tac_toe.engine import TicTacToeEngine


@pytest.fixture
def players():
    u1 = User(id=201, username="player1", first_name="Player 1")
    u2 = User(id=202, username="player2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=201, team="A", group_id=-2001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=202, team="B", group_id=-2002)
    mp2.user = u2
    return [mp1, mp2]


def test_ttt_initial_state(players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(players, mode="solo")

    assert state["game"] == "tic_tac_toe"
    assert len(state["board"]) == 9
    assert all(c is None for c in state["board"])
    assert state["current_player_id"] in (201, 202)

    active_pid = str(state["current_player_id"])
    assert state["players"][active_pid]["symbol"] == "X"


def test_ttt_validation_and_alternating_turns(players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(players, mode="solo")

    turn_1 = state["current_player_id"]
    turn_2 = 202 if turn_1 == 201 else 201

    # Inactive player cannot move
    is_valid, reason = engine.validate_action(state, turn_2, {"cell": 0})
    assert not is_valid
    assert "هنوز نوبت شما نشده است" in reason

    # Out of bounds cell
    is_valid, reason = engine.validate_action(state, turn_1, {"cell": 10})
    assert not is_valid
    assert "نامعتبر" in reason

    # Active player moves to cell 0
    is_valid, reason = engine.validate_action(state, turn_1, {"cell": 0})
    assert is_valid
    state = engine.apply_action(state, turn_1, {"cell": 0})

    assert state["board"][0] == "X"
    assert state["current_player_id"] == turn_2

    # Cannot play in already occupied cell 0
    is_valid, reason = engine.validate_action(state, turn_2, {"cell": 0})
    assert not is_valid
    assert "قبلاً پر شده است" in reason


def test_ttt_horizontal_win(players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(players, mode="solo")

    p_x = state["current_player_id"]
    p_o = 202 if p_x == 201 else 201
    x_team = state["players"][str(p_x)]["team"]

    # Moves sequence:
    # X: 0, O: 3
    # X: 1, O: 4
    # X: 2 -> X wins on top row (0, 1, 2)
    state = engine.apply_action(state, p_x, {"cell": 0})
    state = engine.apply_action(state, p_o, {"cell": 3})
    state = engine.apply_action(state, p_x, {"cell": 1})
    state = engine.apply_action(state, p_o, {"cell": 4})
    state = engine.apply_action(state, p_x, {"cell": 2})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == x_team
    assert "پیروز شد" in result.summary


def test_ttt_diagonal_win(players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(players, mode="solo")

    p_x = state["current_player_id"]
    p_o = 202 if p_x == 201 else 201
    x_team = state["players"][str(p_x)]["team"]

    # Diagonal (0, 4, 8)
    state = engine.apply_action(state, p_x, {"cell": 0})
    state = engine.apply_action(state, p_o, {"cell": 1})
    state = engine.apply_action(state, p_x, {"cell": 4})
    state = engine.apply_action(state, p_o, {"cell": 2})
    state = engine.apply_action(state, p_x, {"cell": 8})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == x_team


def test_ttt_draw(players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(players, mode="solo")

    p_x = state["current_player_id"]
    p_o = 202 if p_x == 201 else 201

    # Board layout for draw:
    # X O X
    # X X O
    # O X O
    # Move order:
    # X: 0, O: 1, X: 2
    # O: 5, X: 3, O: 6
    # X: 4, O: 8, X: 7
    moves = [
        (p_x, 0), (p_o, 1), (p_x, 2),
        (p_o, 5), (p_x, 3), (p_o, 6),
        (p_x, 4), (p_o, 8), (p_x, 7)
    ]
    for pid, cell in moves:
        state = engine.apply_action(state, pid, {"cell": cell})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert result.is_draw
    assert result.winner_team is None
    assert "مساوی" in result.summary


def test_ttt_rendering_and_keyboard(players):
    engine = TicTacToeEngine()
    state = engine.create_initial_state(players, mode="solo")

    p_x = state["current_player_id"]
    p_o = 202 if p_x == 201 else 201

    text = engine.render_for_group(state, -2001, p_x)
    assert "بازی دوز" in text
    assert "نوبت شماست" in text

    kb_active = engine.get_keyboard(state, 1, p_x)
    assert len(kb_active.inline_keyboard) == 3
    assert len(kb_active.inline_keyboard[0]) == 3
    assert kb_active.inline_keyboard[0][0].callback_data == "ttt_action:1:0"

    # Inactive player keyboard has noop buttons
    kb_inactive = engine.get_keyboard(state, 1, p_o)
    assert kb_inactive.inline_keyboard[0][0].callback_data == "noop"
