"""Tests for player cards (Phase 10).

Acquisition, one card per character, favorites, summary. Upgrades are
covered by tests/test_card_upgrade.py (the manual-allocation tests were
removed together with the feature).
"""

import pytest

from sib.database.models import Character
from sib.services.card_progression import card_attributes
from sib.services.characters import CharacterService
from sib.services.player_cards import (
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
async def test_acquire_starts_at_level_one_with_base_attributes(db_session):
    user = await create_test_user(db_session, "کارت‌باز ۱")
    character = await _make_character(db_session, power=22, speed=20, intelligence=18)

    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    assert card.level == 1
    stats = card_attributes(card, character)
    assert (stats.power, stats.speed, stats.intelligence) == (22, 20, 18)
    assert stats.total == 60


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
    assert "هنوز کارتی نداری" in await PlayerCardService.summary(db_session, empty_user.id)


