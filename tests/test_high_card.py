"""Tests for High Card game engine and rules."""

import pytest
from goje.database.models import User, MatchPlayer
from goje.games.high_card.engine import HighCardEngine


@pytest.fixture
def players():
    u1 = User(id=101, username="player1", first_name="Player 1")
    u2 = User(id=102, username="player2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=101, team="A", group_id=-1001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=102, team="B", group_id=-1002)
    mp2.user = u2
    return [mp1, mp2]


def test_high_card_initial_state(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    assert state["game"] == "high_card"
    assert state["mode"] == "solo"
    assert not state["finished"]
    assert "101" in state["players"]
    assert "102" in state["players"]

    p1_data = state["players"]["101"]
    assert p1_data["card_rank"] is None
    assert p1_data["card_suit"] is None
    assert not p1_data["locked"]


def test_high_card_validation(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Non-player validation
    is_valid, reason = engine.validate_action(state, 999, {})
    assert not is_valid
    assert "شما بازیکن این مسابقه نیستید" in reason

    # Valid draw action
    is_valid, reason = engine.validate_action(state, 101, {})
    assert is_valid
    assert reason is None


def test_high_card_lock_and_reveal_higher_rank_wins(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Player 1 draws Rank 14 (Ace)
    state = engine.apply_action(state, 101, {"rank": 14, "suit": "♠️"})
    assert state["players"]["101"]["locked"] is True
    assert not engine.is_finished(state)

    # Player 1 cannot draw again
    is_valid, reason = engine.validate_action(state, 101, {})
    assert not is_valid
    assert "شما قبلاً کارت خود را کشیده‌اید" in reason

    # Player 2 draws Rank 13 (King)
    state = engine.apply_action(state, 102, {"rank": 13, "suit": "♦️"})
    assert state["players"]["102"]["locked"] is True
    assert engine.is_finished(state)

    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == "A"
    assert "Player 1" in result.summary


def test_high_card_draw_on_equal_rank(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Set both players to rank 10 with different suits
    engine.apply_action(state, 101, {"rank": 10, "suit": "♠️"})
    engine.apply_action(state, 102, {"rank": 10, "suit": "♥️"})

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert result.is_draw
    assert result.winner_team is None
    assert "مساوی" in result.summary


def test_high_card_rendering_and_keyboard(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Before lock
    text = engine.render_for_group(state, -1001, 101)
    assert "مسابقه کارت بالا" in text
    assert "برای کشیدن کارت دکمه زیر را لمس کنید" in text

    kb = engine.get_keyboard(state, 1, 101)
    assert len(kb.inline_keyboard) == 1
    assert kb.inline_keyboard[0][0].callback_data == "hc_action:1:draw"

    # After lock
    state = engine.apply_action(state, 101, {"rank": 14, "suit": "♠️"})
    text_locked = engine.render_for_group(state, -1001, 101)
    assert "کارت شما کشیده و قفل شد" in text_locked

    kb_locked = engine.get_keyboard(state, 1, 101)
    assert "🔒" in kb_locked.inline_keyboard[0][0].text
