"""Schema/default regressions for the Friendly match foundation."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from sib.database.models import (
    FriendlyMatchReward,
    Game,
    Group,
    Match,
    MatchType,
    Party,
    PartyMember,
    User,
)
from sib.services.config_store import ConfigService


def test_friendly_reward_defaults_are_tunable():
    assert ConfigService.default("friendly_match_reward_apples") == 10
    assert ConfigService.default("friendly_match_daily_match_cap") == 5


def test_friendly_schema_has_safe_competitive_defaults_and_idempotency_keys():
    assert Party.__table__.c.match_type.default.arg == MatchType.COMPETITIVE
    assert PartyMember.__table__.c.team.default.arg == 1
    assert Match.__table__.c.match_type.default.arg == MatchType.COMPETITIVE
    assert "winner_team" in Match.__table__.c
    assert "rematch_of_match_id" in Match.__table__.c

    unique_keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in FriendlyMatchReward.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("match_id", "user_id") in unique_keys
    assert "trade_selections" in Match.metadata.tables
    assert Match.metadata.tables["trade_selections"].primary_key.columns.keys() == ["token"]


@pytest.mark.asyncio
async def test_existing_party_and_match_orm_defaults_remain_competitive(db_session):
    suffix = uuid.uuid4().int % 1_000_000_000
    user = User(id=700_000_000 + suffix, first_name="بازیکن آزمون")
    group = Group(id=-(700_000_000 + suffix), title="گروه آزمون Friendly")
    db_session.add_all([user, group])
    game = await db_session.scalar(select(Game).where(Game.code == "connect_four"))
    assert game is not None

    party = Party(
        group_id=group.id,
        game_id=game.id,
        mode="solo",
        max_players=1,
        current_players_count=1,
        status="FULL",
        creator_user_id=user.id,
    )
    db_session.add(party)
    await db_session.flush()
    member = PartyMember(party_id=party.id, user_id=user.id)
    match = Match(game_id=game.id, mode="solo", party1_id=party.id, group1_id=group.id)
    db_session.add_all([member, match])
    await db_session.flush()

    assert party.match_type == MatchType.COMPETITIVE
    assert member.team == 1
    assert match.match_type == MatchType.COMPETITIVE
    assert match.winner_team is None
    assert match.rematch_of_match_id is None

    claim = FriendlyMatchReward(
        match_id=match.id,
        user_id=user.id,
        reward_date=date(2026, 9, 30),
        amount=10,
    )
    db_session.add(claim)
    await db_session.flush()
    assert claim.reward_date == date(2026, 9, 30)
