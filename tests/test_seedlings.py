"""Tests for the seedling reward system (Phase 6).

Rules (spec E):
- A normal eligible group-game win awards exactly 1 seedling 🌱.
- A loss awards 0 seedlings.
- A bot-match win awards 0 seedlings.
- Duplicate rewards on retries/reprocessing are impossible.
"""

import pytest
from sqlalchemy import select

from sib.database.models import SeedlingAward, SeedlingInventory
from sib.games.registry import get_game_engine
from sib.matchmaking.acceptance import AcceptanceService
from sib.matchmaking.matcher import MatchmakingService
from sib.services.match_result import MatchResultService
from sib.services.party import PartyService
from sib.services.seedling import SeedlingService
from sib.tests.test_group_wins import _create_match, _winning_state
from sib.tests.test_parties import create_test_user


async def _seedling_total(db_session, user_id):
    return await SeedlingService.get_total(db_session, user_id)


@pytest.mark.asyncio
async def test_normal_win_awards_exactly_one_seedling(db_session):
    match, u1, u2 = await _create_match(db_session)

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    assert await _seedling_total(db_session, u1.id) == 1
    assert await _seedling_total(db_session, u2.id) == 0


@pytest.mark.asyncio
async def test_bot_match_win_awards_zero_seedlings(db_session):
    match, u1, u2 = await _create_match(db_session)
    match.is_bot_match = True
    await db_session.commit()

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    assert await _seedling_total(db_session, u1.id) == 0
    assert await _seedling_total(db_session, u2.id) == 0


@pytest.mark.asyncio
async def test_seedling_award_is_idempotent(db_session):
    match, u1, u2 = await _create_match(db_session)

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    # Simulate a duplicated callback/retry: applying again must be a no-op.
    await MatchResultService.apply_match_result(db_session, match, result)
    # Even a direct duplicate service call is rejected.
    awarded_again = await SeedlingService.award_match_win(db_session, match.id, u1.id)
    await db_session.commit()

    assert awarded_again is False
    assert await _seedling_total(db_session, u1.id) == 1


@pytest.mark.asyncio
async def test_seedling_award_ledger_is_auditable(db_session):
    match, u1, u2 = await _create_match(db_session)

    engine = get_game_engine("high_card")
    state = _winning_state(engine, match, u1, u2)
    result = engine.get_result(state)

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    awards = (
        await db_session.scalars(
            select(SeedlingAward).where(SeedlingAward.match_id == match.id)
        )
    ).all()
    assert len(awards) == 1
    assert awards[0].user_id == u1.id
    assert awards[0].amount == 1
    assert awards[0].reason == "match_win"

    inv = await db_session.scalar(
        select(SeedlingInventory).where(SeedlingInventory.user_id == u1.id)
    )
    assert inv.total == 1


@pytest.mark.asyncio
async def test_team_win_awards_each_winning_player(db_session):
    """2v2: every player of the winning team gets exactly one seedling."""
    from sib.tests.test_parties import create_test_group

    g1 = await create_test_group(db_session, "گروه تیمی ۱")
    g2 = await create_test_group(db_session, "گروه تیمی ۲")
    ua1 = await create_test_user(db_session, "تیمی الف ۱")
    ua2 = await create_test_user(db_session, "تیمی الف ۲")
    ub1 = await create_test_user(db_session, "تیمی ب ۱")
    ub2 = await create_test_user(db_session, "تیمی ب ۲")

    p1 = await PartyService.create_party(db_session, g1.id, ua1.id, "connect_four", "2v2")
    await PartyService.join_party(db_session, p1.id, ua2.id)
    p2 = await PartyService.create_party(db_session, g2.id, ub1.id, "connect_four", "2v2")
    await PartyService.join_party(db_session, p2.id, ub2.id)
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, ua1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, ub1.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    # One accept per team is enough to start a team match.
    await AcceptanceService.accept_match(db_session, match.id, ua1.id)
    await AcceptanceService.accept_match(db_session, match.id, ub1.id)
    await db_session.commit()

    from sib.games.base import GameResult

    winning_team = match.players[0].team  # arbitrary but consistent
    result = GameResult(
        winner_team=winning_team,
        is_draw=False,
        summary="تست",
        details={},
    )
    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    for p in match.players:
        expected = 1 if p.team == winning_team else 0
        assert await _seedling_total(db_session, p.user_id) == expected
