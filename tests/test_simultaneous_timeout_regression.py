"""Regression tests for simultaneous-game timeout and recovery semantics."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from sib.database.models import Match, MatchState, Rating
from sib.services.timeout import TimeoutService
from sib.services.recovery import RecoveryService
from sib.services.party import PartyService
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.tests.test_parties import create_test_user, create_test_group


async def create_active_match(
    session,
    game_code: str,
    user1_name: str,
    user2_name: str,
):
    g1 = await create_test_group(session, f"{game_code} timeout ۱")
    g2 = await create_test_group(session, f"{game_code} timeout ۲")
    u1 = await create_test_user(session, user1_name)
    u2 = await create_test_user(session, user2_name)

    p1 = await PartyService.create_party(
        session, g1.id, u1.id, game_code, "solo"
    )
    p2 = await PartyService.create_party(
        session, g2.id, u2.id, game_code, "solo"
    )
    await session.commit()

    await MatchmakingService.enqueue_party(session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(session, p2.id)

    await AcceptanceService.accept_match(session, match.id, u1.id)
    await AcceptanceService.accept_match(session, match.id, u2.id)

    await session.refresh(match)
    return match, u1, u2


@pytest.mark.asyncio
async def test_high_card_timeout_awards_locked_player_and_rating(db_session):
    """Expired High Card round with one locked player awards that player's team."""
    match, u1, u2 = await create_active_match(
        db_session,
        "high_card",
        "High Card 1",
        "High Card 2",
    )

    match.state = MatchState.ACTIVE
    match.current_turn_player_id = None
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    match.game_state = json.dumps(
        {
            "game": "high_card",
            "mode": "solo",
            "players": {
                str(u1.id): {
                    "team": 1,
                    "group_id": match.group1_id,
                    "name": "High Card 1",
                    "card_rank": 14,
                    "card_suit": "spades",
                    "locked": True,
                },
                str(u2.id): {
                    "team": 2,
                    "group_id": match.group2_id,
                    "name": "High Card 2",
                    "card_rank": None,
                    "card_suit": None,
                    "locked": False,
                },
            },
            "finished": False,
        }
    )
    await db_session.commit()

    processed_ids = await TimeoutService.process_turn_timeouts(db_session)

    assert match.id in processed_ids

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.rating_applied is True
    assert match.winner_group_id == match.group1_id

    r1 = await db_session.scalar(
        select(Rating).where(Rating.group_id == match.group1_id)
    )
    r2 = await db_session.scalar(
        select(Rating).where(Rating.group_id == match.group2_id)
    )
    assert r1.matches_played == 1
    assert r2.matches_played == 1


@pytest.mark.asyncio
async def test_rps_timeout_with_no_locked_players_is_draw(db_session):
    """Expired RPS round with no choices is resolved as a draw."""
    match, u1, u2 = await create_active_match(
        db_session,
        "rock_paper_scissors",
        "RPS 1",
        "RPS 2",
    )

    match.state = MatchState.ACTIVE
    match.current_turn_player_id = None
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    match.game_state = json.dumps(
        {
            "game": "rock_paper_scissors",
            "mode": "solo",
            "players": {
                str(u1.id): {
                    "team": 1,
                    "group_id": match.group1_id,
                    "name": "RPS 1",
                    "choice": None,
                    "locked": False,
                    "score": 0,
                },
                str(u2.id): {
                    "team": 2,
                    "group_id": match.group2_id,
                    "name": "RPS 2",
                    "choice": None,
                    "locked": False,
                    "score": 0,
                },
            },
            "round": 1,
            "total_rounds": 3,
            "finished": False,
        }
    )
    await db_session.commit()

    processed_ids = await TimeoutService.process_turn_timeouts(db_session)

    assert match.id in processed_ids

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.rating_applied is True
    assert match.is_draw is True
    assert match.winner_group_id is None


@pytest.mark.asyncio
async def test_recovery_finishes_expired_simultaneous_match_with_rating(db_session):
    """Recovery must resolve expired simultaneous matches instead of cancelling them."""
    match, u1, u2 = await create_active_match(
        db_session,
        "high_card",
        "Recovery 1",
        "Recovery 2",
    )

    match.state = MatchState.ACTIVE
    match.current_turn_player_id = None
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
    match.game_state = json.dumps(
        {
            "game": "high_card",
            "mode": "solo",
            "players": {
                str(u1.id): {
                    "team": 1,
                    "group_id": match.group1_id,
                    "name": "Recovery 1",
                    "card_rank": None,
                    "card_suit": None,
                    "locked": False,
                },
                str(u2.id): {
                    "team": 2,
                    "group_id": match.group2_id,
                    "name": "Recovery 2",
                    "card_rank": None,
                    "card_suit": None,
                    "locked": False,
                },
            },
            "finished": False,
        }
    )
    await db_session.commit()

    stats = await RecoveryService.recover_on_startup(db_session)

    assert stats["expired_abandoned_matches"] >= 1

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.rating_applied is True
    assert match.is_draw is True

    r1 = await db_session.scalar(
        select(Rating).where(Rating.group_id == match.group1_id)
    )
    r2 = await db_session.scalar(
        select(Rating).where(Rating.group_id == match.group2_id)
    )
    assert r1.matches_played == 1
    assert r2.matches_played == 1
