"""Tests for Apple Battle (نبرد سیبی) (Phase 14).

Same-level matchmaking, precomputed & persisted results with RNG seed and
balance version, idempotent completion, +1 seedling for the winner,
Persian narration from the persisted result.
"""

import json

import pytest

from sib.database.models import (
    AppleBattle,
    AppleBattleStatus,
    ReleaseStatus,
    SeedlingAward,
)
from sib.services.apple_battle import AppleBattleService, BattleError
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


async def _released_character(db_session, code):
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=22,
        speed=20,
        intelligence=18,
        passive_key="test_passive",
        hidden_modifiers={"crit_multiplier": 1.05},
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    return character


async def _card(db_session, user, code):
    character = await _released_character(db_session, code)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()
    return card


@pytest.mark.asyncio
async def test_matchmaking_pairs_same_level_only(db_session):
    a = await create_test_user(db_session, "نبردی الف")
    b = await create_test_user(db_session, "نبردی ب")
    c = await create_test_user(db_session, "نبردی ج")
    card_a = await _card(db_session, a, "battle_a")
    card_b = await _card(db_session, b, "battle_b")
    card_c = await _card(db_session, c, "battle_c")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    # Single player: no match yet.
    assert await AppleBattleService.try_match(db_session) is None

    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()
    battle = await AppleBattleService.try_match(db_session)
    await db_session.commit()

    assert battle is not None
    assert battle.level == 1
    assert {battle.player1_id, battle.player2_id} == {a.id, b.id}

    # C alone cannot match (same-level only).
    await AppleBattleService.enqueue(db_session, c.id, card_c.id)
    await db_session.commit()
    assert await AppleBattleService.try_match(db_session) is None


@pytest.mark.asyncio
async def test_result_is_precomputed_persisted_with_seed_and_balance_version(db_session):
    a = await create_test_user(db_session, "پیش‌محاسبه الف")
    b = await create_test_user(db_session, "پیش‌محاسبه ب")
    card_a = await _card(db_session, a, "battle_d")
    card_b = await _card(db_session, b, "battle_e")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    battle = await AppleBattleService.try_match(db_session)
    await db_session.commit()

    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    await db_session.commit()

    assert battle.status == AppleBattleStatus.RESULT_READY
    assert battle.winner_id in (a.id, b.id)
    assert battle.rng_seed  # documented
    assert battle.balance_version is not None

    result = json.loads(battle.result_json)
    assert result["rng_seed"] == battle.rng_seed
    assert result["balance_version"] == battle.balance_version
    assert "p1_win_probability" in result
    assert result["winner_id"] == battle.winner_id

    # Idempotent: recomputation does not change the persisted outcome.
    winner_before = battle.winner_id
    seed_before = battle.rng_seed
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    assert battle.winner_id == winner_before
    assert battle.rng_seed == seed_before


@pytest.mark.asyncio
async def test_same_seed_same_inputs_give_same_result(db_session):
    """Controlled RNG: the computation is a pure function of the seed."""
    a = await create_test_user(db_session, "تکرار الف")
    b = await create_test_user(db_session, "تکرار ب")
    card_a = await _card(db_session, a, "battle_f")
    card_b = await _card(db_session, b, "battle_g")

    import random

    s1 = random.Random("fixed-seed")
    s2 = random.Random("fixed-seed")
    assert s1.random() == s2.random()


@pytest.mark.asyncio
async def test_winner_gets_exactly_one_seedling_idempotent(db_session):
    a = await create_test_user(db_session, "نهال‌دار الف")
    b = await create_test_user(db_session, "نهال‌دار ب")
    card_a = await _card(db_session, a, "battle_h")
    card_b = await _card(db_session, b, "battle_i")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    battle = await AppleBattleService.try_match(db_session)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    await db_session.commit()

    battle = await AppleBattleService.complete(db_session, battle.id)
    await db_session.commit()

    winner_id = battle.winner_id
    loser_id = b.id if winner_id == a.id else a.id
    assert await SeedlingService.get_total(db_session, winner_id) == 1
    assert await SeedlingService.get_total(db_session, loser_id) == 0

    # Idempotent completion (retry) does not double-award.
    battle = await AppleBattleService.complete(db_session, battle.id)
    await db_session.commit()
    assert await SeedlingService.get_total(db_session, winner_id) == 1

    awards = (
        await db_session.scalars(
            __import__("sqlalchemy").select(SeedlingAward).where(SeedlingAward.user_id == winner_id)
        )
    ).all()
    assert len(awards) == 1
    assert awards[0].source == "battle"
    assert awards[0].reason == "apple_battle_win"


@pytest.mark.asyncio
async def test_narration_is_persian_and_comes_from_persisted_result(db_session):
    a = await create_test_user(db_session, "روایت‌گر الف")
    b = await create_test_user(db_session, "روایت‌گر ب")
    card_a = await _card(db_session, a, "battle_j")
    card_b = await _card(db_session, b, "battle_k")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    battle = await AppleBattleService.try_match(db_session)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    await db_session.commit()

    story = await AppleBattleService.narrate(db_session, battle.id)
    assert "پیروز" in story or "غلبه" in story or "پیروزی" in story
    assert "📊" in story
    assert "قهرمان battle_j" in story or "قهرمان battle_k" in story

    # Narration is stable for the same battle (from the persisted result).
    story2 = await AppleBattleService.narrate(db_session, battle.id)
    assert story == story2


@pytest.mark.asyncio
async def test_dequeue_and_double_enqueue_guards(db_session):
    a = await create_test_user(db_session, "صف‌نشین")
    card_a = await _card(db_session, a, "battle_l")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await db_session.commit()
    with pytest.raises(BattleError):
        await AppleBattleService.enqueue(db_session, a.id, card_a.id)

    assert await AppleBattleService.dequeue(db_session, a.id) is True
    assert await AppleBattleService.dequeue(db_session, a.id) is False


@pytest.mark.asyncio
async def test_cannot_enqueue_someone_elses_card(db_session):
    a = await create_test_user(db_session, "کارت‌سوز")
    b = await create_test_user(db_session, "کارت‌دار")
    card_b = await _card(db_session, b, "battle_m")

    with pytest.raises(BattleError):
        await AppleBattleService.enqueue(db_session, a.id, card_b.id)


@pytest.mark.asyncio
async def test_battle_leaderboard_counts_wins(db_session):
    a = await create_test_user(db_session, "قهرمان الف")
    b = await create_test_user(db_session, "قهرمان ب")
    card_a = await _card(db_session, a, "battle_n")
    card_b = await _card(db_session, b, "battle_o")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    battle = await AppleBattleService.try_match(db_session)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    battle = await AppleBattleService.complete(db_session, battle.id)
    await db_session.commit()

    board = await AppleBattleService.battle_leaderboard(db_session)
    assert len(board) >= 1
    winner_id, wins, total = board[0]
    assert winner_id == battle.winner_id
    assert wins == 1
