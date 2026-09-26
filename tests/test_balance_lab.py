"""Tests for the Balance Lab (Phase 16)."""

import pytest

from sib.database.models import ReleaseStatus
from sib.services.balance_lab import BalanceLabService
from sib.services.characters import CharacterService
from sib.tests.test_parties import create_test_user  # noqa: F401  (fixture hygiene)


@pytest.mark.asyncio
async def test_simulation_is_reproducible_with_same_seed():
    report1 = BalanceLabService.simulate_matchup(
        stats_a={"power": 22, "speed": 20, "intelligence": 18},
        stats_b={"power": 18, "speed": 18, "intelligence": 24},
        rounds=10_000,
        seed="fixed-seed",
    )
    report2 = BalanceLabService.simulate_matchup(
        stats_a={"power": 22, "speed": 20, "intelligence": 18},
        stats_b={"power": 18, "speed": 18, "intelligence": 24},
        rounds=10_000,
        seed="fixed-seed",
    )
    assert report1 == report2
    assert report1["wins_a"] + report1["wins_b"] == 10_000


@pytest.mark.asyncio
async def test_stronger_side_wins_more_often():
    report = BalanceLabService.simulate_matchup(
        stats_a={"power": 40, "speed": 40, "intelligence": 40},
        stats_b={"power": 10, "speed": 10, "intelligence": 10},
        rounds=50_000,
        seed="imbalance",
    )
    assert report["win_rate_a"] > 0.8
    assert report["score_a"] > report["score_b"]


@pytest.mark.asyncio
async def test_100k_simulations_scale(db_session):
    a = await CharacterService.create(
        db_session, code="lab_a", name_fa="آزمایشگاه الف", power=20, speed=20, intelligence=20
    )
    b = await CharacterService.create(
        db_session, code="lab_b", name_fa="آزمایشگاه ب", power=22, speed=18, intelligence=20,
        hidden_modifiers={"crit_multiplier": 1.05},
    )
    await db_session.commit()

    report = await BalanceLabService.simulate_characters(
        db_session, a.id, b.id, rounds=100_000, seed="balance-lab"
    )
    assert report["rounds"] == 100_000
    assert report["wins_a"] + report["wins_b"] == 100_000
    assert report["character_a"]["code"] == "lab_a"
    assert report["character_b"]["balance_version"] == 1
    # Hidden modifiers are part of the model.
    assert report["score_b"] > 0


@pytest.mark.asyncio
async def test_invalid_rounds_rejected():
    with pytest.raises(ValueError):
        BalanceLabService.simulate_matchup(
            stats_a={"power": 20, "speed": 20, "intelligence": 20},
            stats_b={"power": 20, "speed": 20, "intelligence": 20},
            rounds=0,
        )
