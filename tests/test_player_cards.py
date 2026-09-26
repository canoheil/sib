"""Tests for player cards (Phase 10).

Budget model: total attributes == level budget (60/120/180/240/300,
extensible). One card per character. Apple-based upgrades.
"""

import pytest

from sib.database.models import Character
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.player_cards import (
    CardError,
    DuplicateCardError,
    PlayerCardService,
)
from sib.tests.test_parties import create_test_user


async def _make_character(db_session, code="hero", power=20, speed=20, intelligence=20):
    return await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=power,
        speed=speed,
        intelligence=intelligence,
    )


@pytest.mark.asyncio
async def test_acquire_starts_at_level_one_with_budget_60(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۱")
    character = await _make_character(db_session, power=22, speed=20, intelligence=18)

    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    assert card.level == 1
    assert card.power == 22
    assert card.power + card.speed + card.intelligence == 60


@pytest.mark.asyncio
async def test_only_one_card_per_character(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۲")
    character = await _make_character(db_session)

    await PlayerCardService.acquire(db_session, user.id, character.id)
    with pytest.raises(DuplicateCardError):
        await PlayerCardService.acquire(db_session, user.id, character.id)

    # A different player may own the same character.
    other = await create_test_user(db_session, "کارت‌باز ۲b")
    card = await PlayerCardService.acquire(db_session, other.id, character.id)
    assert card.user_id == other.id


@pytest.mark.asyncio
async def test_level_budgets_and_future_levels(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۳")

    assert await PlayerCardService.level_budget(db_session, 1) == 60
    assert await PlayerCardService.level_budget(db_session, 3) == 180
    assert await PlayerCardService.level_budget(db_session, 5) == 300
    # Levels beyond the configured list extrapolate (+60 per level).
    assert await PlayerCardService.level_budget(db_session, 6) == 360
    assert await PlayerCardService.level_budget(db_session, 10) == 600


@pytest.mark.asyncio
async def test_upgrade_cost_is_config_driven(db_session):
    # base=100, multiplier=1.5 → L1→L2 costs 100, L2→L3 costs 150.
    assert await PlayerCardService.upgrade_cost(db_session, 1) == 100
    assert await PlayerCardService.upgrade_cost(db_session, 2) == 150
    assert await PlayerCardService.upgrade_cost(db_session, 3) == 225


@pytest.mark.asyncio
async def test_upgrade_spends_apples_and_keeps_budget(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۴")
    character = await _make_character(db_session)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    await AppleWalletService.earn(db_session, user.id, 200, reason="test_grant")
    await db_session.commit()

    # Wrong allocation total rejected.
    with pytest.raises(CardError):
        await PlayerCardService.upgrade(
            db_session, user.id, card.id, boost_power=5, boost_speed=0, boost_intelligence=0
        )

    # Level 1→2: budget 60→120 → exactly 60 new points.
    card = await PlayerCardService.upgrade(
        db_session, user.id, card.id, boost_power=30, boost_speed=20, boost_intelligence=10
    )
    await db_session.commit()

    assert card.level == 2
    assert card.power + card.speed + card.intelligence == 120
    assert card.boosted_power == 30
    assert await AppleWalletService.get_balance(db_session, user.id) == 100  # 200-100

    # Insufficient funds for the next upgrade (150 apples, have 100).
    with pytest.raises(CardError):
        await PlayerCardService.upgrade(
            db_session, user.id, card.id, boost_power=20, boost_speed=20, boost_intelligence=20
        )


@pytest.mark.asyncio
async def test_reallocate_enforces_exact_budget(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۵")
    character = await _make_character(db_session)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    with pytest.raises(CardError):
        await PlayerCardService.reallocate(
            db_session, user.id, card.id, power=10, speed=10, intelligence=10
        )

    card = await PlayerCardService.reallocate(
        db_session, user.id, card.id, power=40, speed=10, intelligence=10
    )
    await db_session.commit()
    assert (card.power, card.speed, card.intelligence) == (40, 10, 10)


@pytest.mark.asyncio
async def test_only_one_favorite_at_a_time(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۶")
    character_a = await _make_character(db_session, code="hero_a")
    character_b = await _make_character(db_session, code="hero_b")
    card_a = await PlayerCardService.acquire(db_session, user.id, character_a.id)
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    await db_session.commit()

    await PlayerCardService.set_favorite(db_session, user.id, card_a.id, True)
    await PlayerCardService.set_favorite(db_session, user.id, card_b.id, True)
    await db_session.commit()

    cards = await PlayerCardService.list_cards(db_session, user.id)
    favorites = [c for c in cards if c.is_favorite]
    assert len(favorites) == 1
    assert favorites[0].id == card_b.id


@pytest.mark.asyncio
async def test_cards_summary_is_persian(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۷")
    character = await _make_character(db_session)
    await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    summary = await PlayerCardService.summary(db_session, user.id)
    assert "کارت‌های شما" in summary
    assert "سطح 1" in summary

    empty_user = await create_test_user(db_session, "کارت‌باز ۷b")
    assert "هنوز کارتی ندارید" in await PlayerCardService.summary(db_session, empty_user.id)


@pytest.mark.asyncio
async def test_cannot_upgrade_someone_elses_card(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۸")
    thief = await create_test_user(db_session, "کارت‌دزد")
    character = await _make_character(db_session)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    await AppleWalletService.earn(db_session, thief.id, 500, reason="test_grant")
    await db_session.commit()

    with pytest.raises(CardError):
        await PlayerCardService.upgrade(
            db_session, thief.id, card.id, boost_power=20, boost_speed=20, boost_intelligence=20
        )
