"""Tests for Rock Paper Scissors gameplay, result evaluation, result application, and idempotency."""

import json
import pytest
from sqlalchemy import select
from sib.core.config import settings
from sib.database.models import (
    Match,
    MatchState,
    GroupStats,
    PlayerStats,
)
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.services.match_result import MatchResultService
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_user, create_test_group


def test_rps_game_rules():
    """Verify Rock Paper Scissors standard winning and draw rules."""
    engine = RockPaperScissorsEngine()

    state = {
        "players": {
            "1": {"team": 1, "group_id": -101, "name": "A", "choice": "rock", "locked": True, "score": 2},
            "2": {"team": 2, "group_id": -102, "name": "B", "choice": "scissors", "locked": True, "score": 0},
        },
        "finished": True,
    }
    # Rock beats Scissors -> Team 1 wins
    r1 = engine.get_result(state)
    assert r1.winner_team == 1
    assert r1.is_draw is False

    # Scissors beats Paper -> Team 1 wins
    state["players"]["1"]["choice"] = "scissors"
    state["players"]["2"]["choice"] = "paper"
    state["players"]["1"]["score"] = 2
    state["players"]["2"]["score"] = 0
    r2 = engine.get_result(state)
    assert r2.winner_team == 1
    assert r2.is_draw is False

    # Paper beats Rock -> Team 1 wins
    state["players"]["1"]["choice"] = "paper"
    state["players"]["2"]["choice"] = "rock"
    state["players"]["1"]["score"] = 2
    state["players"]["2"]["score"] = 0
    r3 = engine.get_result(state)
    assert r3.winner_team == 1
    assert r3.is_draw is False

    # Team 2 wins: Rock (team 2) beats Scissors (team 1)
    state["players"]["1"]["choice"] = "scissors"
    state["players"]["2"]["choice"] = "rock"
    state["players"]["1"]["score"] = 0
    state["players"]["2"]["score"] = 2
    r4 = engine.get_result(state)
    assert r4.winner_team == 2
    assert r4.is_draw is False

    # Same choices -> Draw
    state["players"]["1"]["choice"] = "rock"
    state["players"]["2"]["choice"] = "rock"
    state["players"]["1"]["score"] = 1
    state["players"]["2"]["score"] = 1
    r5 = engine.get_result(state)
    assert r5.winner_team is None
    assert r5.is_draw is True


def test_rps_action_validation():
    """Verify actions cannot be duplicated once locked or made by non-players."""
    engine = RockPaperScissorsEngine()

    state = {
        "players": {
            "100": {"team": 1, "group_id": -1, "name": "P1", "choice": None, "locked": False},
            "200": {"team": 2, "group_id": -2, "name": "P2", "choice": None, "locked": False},
        },
        "finished": False,
    }

    # Valid choice
    valid, err = engine.validate_action(state, 100, {"choice": "rock"})
    assert valid is True
    assert err is None

    # Invalid choice value
    valid, err = engine.validate_action(state, 100, {"choice": "laser_gun"})
    assert valid is False
    assert "نامعتبر" in err

    # Non-player
    valid, err = engine.validate_action(state, 999, {"choice": "rock"})
    assert valid is False
    assert "نیستید" in err

    # Apply action and test locking
    engine.apply_action(state, 100, {"choice": "rock"})
    assert state["players"]["100"]["locked"] is True

    # Cannot change choice once locked
    valid, err = engine.validate_action(state, 100, {"choice": "paper"})
    assert valid is False
    assert "قفل" in err




@pytest.mark.asyncio
async def test_result_application_idempotency(db_session):
    """Calling apply_match_result twice must NEVER apply the result twice."""
    g1 = await create_test_group(db_session, "تیم تست ریتینگ ۱")
    g2 = await create_test_group(db_session, "تیم تست ریتینگ ۲")
    u1 = await create_test_user(db_session, "بازیکن ریتینگ ۱")
    u2 = await create_test_user(db_session, "بازیکن ریتینگ ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)
    await db_session.commit()

    engine = RockPaperScissorsEngine()
    state = engine.create_initial_state(match.players, match.mode)
    # P1 chooses rock, P2 chooses scissors
    engine.apply_action(state, u1.id, {"choice": "rock"})
    engine.apply_action(state, u2.id, {"choice": "scissors"})
    result = engine.get_result(state)

    # First application
    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    # Get group stats after first application
    g1_stats_first = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == match.group1_id))
    g2_stats_first = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == match.group2_id))
    wins_g1_first = g1_stats_first.wins
    matches_g1_first = g1_stats_first.matches_played

    # Second application (must be completely ignored due to idempotency)
    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    g1_stats_second = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == match.group1_id))
    g2_stats_second = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == match.group2_id))

    assert g1_stats_second.wins == wins_g1_first
    assert g2_stats_second.wins == g2_stats_first.wins
    assert g1_stats_second.matches_played == matches_g1_first


@pytest.mark.asyncio
async def test_full_rock_paper_scissors_lifecycle(db_session):
    """
    End-to-end integration test:
    Parties -> Matchmaking -> Acceptance -> Gameplay choices -> Result application -> Player Stats.
    """
    g1 = await create_test_group(db_session, "گروه ستاره")
    g2 = await create_test_group(db_session, "گروه خورشید")
    u1 = await create_test_user(db_session, "ستاره")
    u2 = await create_test_user(db_session, "خورشید")

    # 1. Parties
    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    # 2. Matchmaking
    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await db_session.commit()

    # 3. Acceptance
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    m, both = await AcceptanceService.accept_match(db_session, match.id, u2.id)
    await db_session.commit()
    assert m.state == MatchState.ACTIVE

    # 4. Gameplay: User 1 chooses Paper, User 2 chooses Rock
    engine = RockPaperScissorsEngine()
    state = engine.create_initial_state(match.players, match.mode)

    # Round 1: Paper beats Rock -> 1-0, match continues.
    engine.apply_action(state, u1.id, {"choice": "paper"})
    assert engine.is_finished(state) is False

    engine.apply_action(state, u2.id, {"choice": "rock"})
    assert engine.is_finished(state) is False
    assert state["players"][str(u1.id)]["score"] == 1
    assert state["players"][str(u2.id)]["score"] == 0

    # Round 2: Paper beats Rock again -> 2-0, match finishes.
    engine.apply_action(state, u1.id, {"choice": "paper"})
    assert engine.is_finished(state) is False

    engine.apply_action(state, u2.id, {"choice": "rock"})
    assert engine.is_finished(state) is True

    # 5. Evaluate Result: Paper beats Rock -> u1 wins!
    result = engine.get_result(state)
    assert result.is_draw is False

    # 6. Apply match result (no Elo)
    finished_match = await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    assert finished_match.state == MatchState.FINISHED
    assert finished_match.result_applied is True

    # 7. Verify Player Statistics
    p1_stats = await db_session.scalar(select(PlayerStats).where(PlayerStats.user_id == u1.id))
    p2_stats = await db_session.scalar(select(PlayerStats).where(PlayerStats.user_id == u2.id))

    assert p1_stats is not None
    assert p1_stats.matches_played == 1
    assert p1_stats.wins == 1
    assert p1_stats.losses == 0

    assert p2_stats is not None
    assert p2_stats.matches_played == 1
    assert p2_stats.wins == 0
    assert p2_stats.losses == 1
