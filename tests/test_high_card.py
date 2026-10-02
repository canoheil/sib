"""Tests for High Card game engine - 5-round match with Swap/Keep mechanic."""

import pytest
from sib.database.models import User, MatchPlayer
from sib.games.high_card.engine import HighCardEngine


@pytest.fixture
def players():
    u1 = User(id=101, username="player1", first_name="Player 1")
    u2 = User(id=102, username="player2", first_name="Player 2")
    mp1 = MatchPlayer(match_id=1, user_id=101, team="A", group_id=-1001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=102, team="B", group_id=-1002)
    mp2.user = u2
    return [mp1, mp2]


def _play_round(engine, state, p1_rank, p1_suit, p2_rank, p2_suit, p1_choice="keep", p2_choice="keep"):
    """Helper: play one full round with predetermined cards and choices."""
    # Player 1 draws
    engine.apply_action(state, 101, {"draw": True, "rank": p1_rank, "suit": p1_suit})
    # Player 2 draws
    engine.apply_action(state, 102, {"draw": True, "rank": p2_rank, "suit": p2_suit})
    # Player 1 decides
    engine.apply_action(state, 101, {"choice": p1_choice})
    # Player 2 decides
    engine.apply_action(state, 102, {"choice": p2_choice})
    return state


# ---------------------------------------------------------------------------
# 1. Initial state
# ---------------------------------------------------------------------------


def test_high_card_initial_state(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    assert state["game"] == "high_card"
    assert state["mode"] == "solo"
    assert not state["finished"]
    assert state["round"] == 1
    assert state["total_rounds"] == 5
    assert "101" in state["players"]
    assert "102" in state["players"]

    p1_data = state["players"]["101"]
    assert p1_data["card_rank"] is None
    assert p1_data["card_suit"] is None
    assert not p1_data["locked"]
    assert p1_data["score"] == 0


# ---------------------------------------------------------------------------
# 2. Player wins a round
# ---------------------------------------------------------------------------


def test_player_wins_round(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Player 1 draws Ace, Player 2 draws King, both keep
    state = _play_round(engine, state, 14, "♠️", 13, "♦️")

    assert state["players"]["101"]["score"] == 1
    assert state["players"]["102"]["score"] == 0
    assert state["round"] == 2  # Moved to round 2
    assert not engine.is_finished(state)


# ---------------------------------------------------------------------------
# 3. Bot wins a round
# ---------------------------------------------------------------------------


def test_bot_wins_round(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Player 1 draws 5, Player 2 draws 12
    state = _play_round(engine, state, 5, "♣️", 12, "♥️")

    assert state["players"]["101"]["score"] == 0
    assert state["players"]["102"]["score"] == 1
    assert state["round"] == 2


# ---------------------------------------------------------------------------
# 4. Draw round (equal ranks)
# ---------------------------------------------------------------------------


def test_draw_round_equal_ranks(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Both draw rank 10
    state = _play_round(engine, state, 10, "♠️", 10, "♥️")

    assert state["players"]["101"]["score"] == 0
    assert state["players"]["102"]["score"] == 0
    assert state["round"] == 2
    assert state["last_round_result"] == "draw"


# ---------------------------------------------------------------------------
# 5. Score updates between rounds
# ---------------------------------------------------------------------------


def test_score_updates_between_rounds(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Round 1: Player 1 wins
    state = _play_round(engine, state, 14, "♠️", 10, "♦️")
    assert state["players"]["101"]["score"] == 1
    assert state["players"]["102"]["score"] == 0

    # Round 2: Player 2 wins
    state = _play_round(engine, state, 5, "♣️", 13, "♥️")
    assert state["players"]["101"]["score"] == 1
    assert state["players"]["102"]["score"] == 1

    # Round 3: Player 1 wins
    state = _play_round(engine, state, 12, "♦️", 8, "♠️")
    assert state["players"]["101"]["score"] == 2
    assert state["players"]["102"]["score"] == 1


# ---------------------------------------------------------------------------
# 6. Match ending on Round 5
# ---------------------------------------------------------------------------


def test_match_ends_on_round_5(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # 2-2 after 4 rounds
    state = _play_round(engine, state, 14, "♠️", 10, "♦️")  # P1 wins R1
    state = _play_round(engine, state, 5, "♣️", 13, "♥️")   # P2 wins R2
    state = _play_round(engine, state, 12, "♦️", 8, "♠️")   # P1 wins R3
    state = _play_round(engine, state, 3, "♣️", 11, "♥️")   # P2 wins R4

    assert not engine.is_finished(state)
    assert state["round"] == 5
    assert state["players"]["101"]["score"] == 2
    assert state["players"]["102"]["score"] == 2

    # Round 5: Player 1 wins → 3-2 final
    state = _play_round(engine, state, 14, "♠️", 7, "♦️")
    assert engine.is_finished(state)

    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == "A"


# ---------------------------------------------------------------------------
# 7. Early match completion (first to 3)
# ---------------------------------------------------------------------------


def test_early_completion_first_to_3(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Player 1 wins 3 straight → match over at round 3
    state = _play_round(engine, state, 14, "♠️", 10, "♦️")
    assert not engine.is_finished(state)

    state = _play_round(engine, state, 13, "♣️", 8, "♥️")
    assert not engine.is_finished(state)

    state = _play_round(engine, state, 12, "♦️", 6, "♠️")
    assert engine.is_finished(state)  # 3-0, match over

    result = engine.get_result(state)
    assert result.winner_team == "A"
    assert result.details["p1_score"] == 3
    assert result.details["p2_score"] == 0


# ---------------------------------------------------------------------------
# 8. Timeout handling (validate_action rejects when finished)
# ---------------------------------------------------------------------------


def test_validation_rejects_action_after_match_finished(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Force match to finish
    state = _play_round(engine, state, 14, "♠️", 2, "♦️")
    state = _play_round(engine, state, 13, "♣️", 3, "♥️")
    state = _play_round(engine, state, 12, "♦️", 4, "♠️")

    assert engine.is_finished(state)

    is_valid, reason = engine.validate_action(state, 101, {"draw": True})
    assert not is_valid
    assert "به پایان رسیده" in reason


# ---------------------------------------------------------------------------
# 9. Cancellation / non-player rejection
# ---------------------------------------------------------------------------


def test_non_player_rejected(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    is_valid, reason = engine.validate_action(state, 999, {"draw": True})
    assert not is_valid
    assert "بازیکن این مسابقه نیستید" in reason


# ---------------------------------------------------------------------------
# 10. Correct settlement (get_result)
# ---------------------------------------------------------------------------


def test_settlement_player_win(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    state = _play_round(engine, state, 14, "♠️", 2, "♦️")
    state = _play_round(engine, state, 13, "♣️", 3, "♥️")
    state = _play_round(engine, state, 12, "♦️", 4, "♠️")

    result = engine.get_result(state)
    assert not result.is_draw
    assert result.winner_team == "A"
    assert result.details["p1_score"] == 3
    assert result.details["p2_score"] == 0
    assert "3 - 0" in result.summary


def test_settlement_draw(players):
    """Match can end in a draw if scores are equal after 5 rounds."""
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # 2 wins each, 1 draw round
    state = _play_round(engine, state, 14, "♠️", 2, "♦️")   # P1
    state = _play_round(engine, state, 2, "♣️", 14, "♥️")   # P2
    state = _play_round(engine, state, 13, "♦️", 3, "♠️")   # P1
    state = _play_round(engine, state, 3, "♣️", 13, "♥️")   # P2
    state = _play_round(engine, state, 10, "♦️", 10, "♠️")  # Draw

    assert engine.is_finished(state)
    result = engine.get_result(state)
    assert result.is_draw
    assert result.winner_team is None
    assert result.details["p1_score"] == 2
    assert result.details["p2_score"] == 2


# ---------------------------------------------------------------------------
# 11. Prevention of invalid / duplicated round state
# ---------------------------------------------------------------------------


def test_cannot_act_twice_in_same_round(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Draw card
    engine.apply_action(state, 101, {"draw": True, "rank": 14, "suit": "♠️"})
    # Keep card
    engine.apply_action(state, 101, {"choice": "keep"})

    # Cannot act again
    is_valid, reason = engine.validate_action(state, 101, {"choice": "keep"})
    assert not is_valid
    assert "قبلاً تصمیم" in reason


def test_must_draw_before_choosing(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Cannot keep/swap without drawing first
    is_valid, reason = engine.validate_action(state, 101, {"choice": "keep"})
    assert not is_valid
    assert "ابتدا باید کارت بکشید" in reason


def test_invalid_choice_rejected(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Draw card first
    engine.apply_action(state, 101, {"draw": True, "rank": 10, "suit": "♠️"})

    # Invalid choice
    is_valid, reason = engine.validate_action(state, 101, {"choice": "invalid"})
    assert not is_valid
    assert "نامعتبر" in reason


# ---------------------------------------------------------------------------
# 12. Swap mechanic
# ---------------------------------------------------------------------------


def test_swap_changes_card(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Draw a specific card
    engine.apply_action(state, 101, {"draw": True, "rank": 3, "suit": "♣️"})
    assert state["players"]["101"]["card_rank"] == 3

    # Swap (new card is random, so we just verify the flag and that it changed or stayed)
    engine.apply_action(state, 101, {"choice": "swap"})
    assert state["players"]["101"]["swapped"] is True
    assert state["players"]["101"]["locked"] is True


def test_keep_preserves_card(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    engine.apply_action(state, 101, {"draw": True, "rank": 14, "suit": "♠️"})
    engine.apply_action(state, 101, {"choice": "keep"})

    assert state["players"]["101"]["card_rank"] == 14
    assert state["players"]["101"]["card_suit"] == "♠️"
    assert state["players"]["101"]["swapped"] is False
    assert state["players"]["101"]["locked"] is True


# ---------------------------------------------------------------------------
# 13. Rendering and keyboard
# ---------------------------------------------------------------------------


def test_rendering_before_draw(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    text = engine.render_for_group(state, -1001, 101)
    assert "کارت بالا" in text
    assert "راند 1 از 5" in text
    assert "امتیاز:** 0 - 0" in text
    assert "کارت بکشید" in text


def test_rendering_after_draw_before_decision(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    engine.apply_action(state, 101, {"draw": True, "rank": 14, "suit": "♠️"})

    text = engine.render_for_group(state, -1001, 101)
    assert "تک / آس (A)" in text
    assert "نگه می‌دارید یا تعویض" in text


def test_rendering_after_lock(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    engine.apply_action(state, 101, {"draw": True, "rank": 14, "suit": "♠️"})
    engine.apply_action(state, 101, {"choice": "keep"})

    text = engine.render_for_group(state, -1001, 101)
    assert "تصمیم ثبت شد" in text


def test_rendering_match_end(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    state = _play_round(engine, state, 14, "♠️", 2, "♦️")
    state = _play_round(engine, state, 13, "♣️", 3, "♥️")
    state = _play_round(engine, state, 12, "♦️", 4, "♠️")

    text = engine.render_for_group(state, -1001, 101)
    assert "نتیجه نهایی" in text
    assert "پیروز شدید" in text


def test_keyboard_draw_phase(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    kb = engine.get_keyboard(state, 1, 101)
    assert len(kb.inline_keyboard) == 1
    assert kb.inline_keyboard[0][0].callback_data == "hc_action:1:draw"


def test_keyboard_decision_phase(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    engine.apply_action(state, 101, {"draw": True, "rank": 10, "suit": "♠️"})

    kb = engine.get_keyboard(state, 1, 101)
    assert len(kb.inline_keyboard) == 2  # Decision row + card display row
    assert kb.inline_keyboard[0][0].callback_data == "hc_action:1:keep"
    assert kb.inline_keyboard[0][1].callback_data == "hc_action:1:swap"


def test_keyboard_locked_phase(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    engine.apply_action(state, 101, {"draw": True, "rank": 14, "suit": "♠️"})
    engine.apply_action(state, 101, {"choice": "keep"})

    kb = engine.get_keyboard(state, 1, 101)
    assert "🔒" in kb.inline_keyboard[0][0].text
    assert kb.inline_keyboard[0][0].callback_data == "noop"


# ---------------------------------------------------------------------------
# 14. Bot strategy
# ---------------------------------------------------------------------------


def test_bot_strategy_keeps_high_card():
    from sib.sib.bots.strategies.high_card import HighCardBotStrategy

    strategy = HighCardBotStrategy()

    state = {
        "players": {
            "999": {"card_rank": 14, "card_suit": "♠️", "locked": False}
        }
    }
    action = strategy.choose_action(state, 999)
    assert action["choice"] == "keep"


def test_bot_strategy_swaps_low_card():
    from sib.sib.bots.strategies.high_card import HighCardBotStrategy

    strategy = HighCardBotStrategy()

    state = {
        "players": {
            "999": {"card_rank": 5, "card_suit": "♣️", "locked": False}
        }
    }
    action = strategy.choose_action(state, 999)
    assert action["choice"] == "swap"


def test_bot_strategy_draws_when_no_card():
    from sib.sib.bots.strategies.high_card import HighCardBotStrategy

    strategy = HighCardBotStrategy()

    state = {
        "players": {
            "999": {"card_rank": None, "card_suit": None, "locked": False}
        }
    }
    action = strategy.choose_action(state, 999)
    assert action.get("draw") is True


# ---------------------------------------------------------------------------
# 15. Last round result display
# ---------------------------------------------------------------------------


def test_last_round_result_shown_in_render(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    # Round 1: Player 1 wins
    state = _play_round(engine, state, 14, "♠️", 5, "♦️")

    # In round 2, the render should mention last round result
    text = engine.render_for_group(state, -1001, 101)
    assert "راند قبل را بردید" in text


# ---------------------------------------------------------------------------
# 16. Round state resets between rounds
# ---------------------------------------------------------------------------


def test_round_state_resets(players):
    engine = HighCardEngine()
    state = engine.create_initial_state(players, mode="solo")

    state = _play_round(engine, state, 14, "♠️", 5, "♦️")

    # After round resolution, per-round state should be reset
    assert state["players"]["101"]["card_rank"] is None
    assert state["players"]["101"]["card_suit"] is None
    assert state["players"]["101"]["locked"] is False
    assert state["players"]["101"]["swapped"] is False
    assert state["players"]["102"]["card_rank"] is None
    assert state["players"]["102"]["locked"] is False

    # But scores persist
    assert state["players"]["101"]["score"] == 1
