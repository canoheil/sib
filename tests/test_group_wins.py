"""Tests for the win-based group leaderboard rules (Phase 5).

Rules:
- A normal eligible group win awards exactly one group win.
- A loss does NOT subtract a win.
- Bot matches must NOT affect group wins or leaderboard position.
- Win award is idempotent.

Matchmaking assigns parties to group1/group2 slots in queue order, so
tests resolve winner/loser groups through the match result itself.
"""

import pytest
from sqlalchemy import select

from sib.database.models import GroupStats
from sib.games.registry import get_game_engine
from sib.matchmaking.acceptance import AcceptanceService
from sib.matchmaking.matcher import MatchmakingService
from sib.services.match_result import MatchResultService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


async def _create_match(db_session, game="high_card"):
    g1 = await create_test_group(db_session, "گروه برد ۱")
    g2 = await create_test_group(db_session, "گروه برد ۲")
    u1 = await create_test_user(db_session, "بازیکن برد ۱")
    u2 = await create_test_user(db_session, "بازیکن برد ۲")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, game, "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, game, "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)
    await db_session.commit()
    return match, u1, u2


async def _stats(db_session, group_id):
    return await db_session.scalar(
        select(GroupStats).where(GroupStats.group_id == group_id)
    )


def _player_team(match, user_id):
    return next(p.team for p in match.players if p.user_id == user_id)


def _group_of_team(match, team):
    return match.group1_id if team == 1 else match.group2_id


def _winning_state(engine, match, winner_user, loser_user):
    """Create a winning state for High Card (best of 5, first to 3 wins)."""
    state = engine.create_initial_state(match.players, match.mode)

    # Play 3 rounds where winner_user wins each
    for _ in range(3):
        # Winner draws Ace, loser draws 2
        engine.apply_action(state, winner_user.id, {"draw": True, "rank": 14, "suit": "♠️"})
        engine.apply_action(state, loser_user.id, {"draw": True, "rank": 2, "suit": "♥️"})
        # Both keep their cards
        engine.apply_action(state, winner_user.id, {"choice": "keep"})
        engine.apply_action(state, loser_user.id, {"choice": "keep"})

    return state


@pytest.mark.asyncio
async def test_normal_win_awards_exactly_one_group_win(db_session):
    match, u1, u2 = await _create_match(db_session)

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)
    assert not result.is_draw

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    winner_group = _group_of_team(match, _player_team(match, u1.id))
    loser_group = _group_of_team(match, _player_team(match, u2.id))

    w = await _stats(db_session, winner_group)
    l = await _stats(db_session, loser_group)

    assert w.wins == 1
    assert w.matches_played == 1
    assert l.wins == 0
    assert l.losses == 1


@pytest.mark.asyncio
async def test_loss_never_subtracts_wins(db_session):
    match, u1, u2 = await _create_match(db_session)

    loser_group = _group_of_team(match, _player_team(match, u2.id))
    pre = await _stats(db_session, loser_group)
    pre.wins = 7  # existing wins from earlier matches
    await db_session.commit()

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    l = await _stats(db_session, loser_group)
    assert l.wins == 7  # untouched by the loss
    assert l.losses == 1


@pytest.mark.asyncio
async def test_bot_match_does_not_affect_group_wins(db_session):
    """Spec D: bot matches never change group wins/leaderboard position."""
    match, u1, u2 = await _create_match(db_session)

    match.is_bot_match = True
    await db_session.commit()

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    g1 = await _stats(db_session, match.group1_id)
    g2 = await _stats(db_session, match.group2_id)

    assert (g1.wins or 0) == 0
    assert (g2.wins or 0) == 0
    assert (g1.matches_played or 0) == 0
    assert (g2.matches_played or 0) == 0


@pytest.mark.asyncio
async def test_group_win_award_is_idempotent(db_session):
    match, u1, u2 = await _create_match(db_session)

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    winner_group = _group_of_team(match, _player_team(match, u1.id))
    w = await _stats(db_session, winner_group)
    assert w.wins == 1
    assert w.matches_played == 1
