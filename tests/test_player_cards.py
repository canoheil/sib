"""Tests for player cards (Phase 10).

Acquisition, one card per character, favorites, summary. Upgrades are
covered by tests/test_card_upgrade.py (the manual-allocation tests were
removed together with the feature).
"""

import asyncio

import pytest

from sib.database.connection import get_async_sessionmaker
from sib.database.models import Character, PlayerCard
from sib.services.card_progression import card_attributes
from sib.services.characters import CharacterService
from sib.services.player_cards import (
    CardError,
    DuplicateCardError,
    PlayerCardService,
    StaleMainCardSelectionError,
)
from sib.tests.conftest import requires_row_locks
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
    assert "کارت اصلی" in await PlayerCardService.summary(db_session, user.id)


@pytest.mark.asyncio
async def test_main_card_is_optional_and_selection_switch_is_idempotent(db_session):
    user = await create_test_user(db_session, "کارت‌باز اصلی")
    character_a = await _make_character(db_session, code="main_a")
    character_b = await _make_character(db_session, code="main_b")
    card_a = await PlayerCardService.acquire(db_session, user.id, character_a.id)
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    await db_session.commit()

    assert await PlayerCardService.get_main_card(db_session, user.id) is None
    assert "کارت اصلی: انتخاب نشده" in await PlayerCardService.summary(db_session, user.id)

    await PlayerCardService.set_favorite(
        db_session, user.id, card_a.id, expected_main_card_id=0
    )
    await db_session.commit()
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == card_a.id

    # Replayed callback with its original expected id does not undo the choice.
    await PlayerCardService.set_favorite(
        db_session, user.id, card_a.id, expected_main_card_id=0
    )
    await db_session.commit()
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == card_a.id
    assert not db_session.is_modified(card_a)

    await PlayerCardService.set_favorite(
        db_session, user.id, card_b.id, expected_main_card_id=card_a.id
    )
    await db_session.commit()
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == card_b.id
    cards = await PlayerCardService.list_cards(db_session, user.id)
    assert [card.id for card in cards if card.is_favorite] == [card_b.id]

    await PlayerCardService.set_favorite(db_session, user.id, card_b.id, False)
    await db_session.commit()
    assert await PlayerCardService.get_main_card(db_session, user.id) is None


@pytest.mark.asyncio
async def test_main_card_rejects_unowned_and_missing_cards(db_session):
    owner = await create_test_user(db_session, "مالک کارت")
    stranger = await create_test_user(db_session, "کاربر دیگر")
    character = await _make_character(db_session, code="main_owned")
    card = await PlayerCardService.acquire(db_session, owner.id, character.id)
    owner_id, stranger_id, card_id = owner.id, stranger.id, card.id
    await db_session.commit()

    with pytest.raises(CardError, match="متعلق به شما نیست"):
        await PlayerCardService.set_favorite(
            db_session, stranger_id, card_id, expected_main_card_id=0
        )
    await db_session.rollback()
    assert await PlayerCardService.get_main_card(db_session, owner_id) is None

    with pytest.raises(CardError, match="متعلق به شما نیست"):
        await PlayerCardService.set_favorite(
            db_session, owner_id, 9_999_999_999, expected_main_card_id=0
        )
    await db_session.rollback()
    assert await PlayerCardService.get_main_card(db_session, owner_id) is None


@pytest.mark.asyncio
async def test_stale_main_card_selection_does_not_replace_current_choice(db_session):
    user = await create_test_user(db_session, "کارت‌باز انتخاب قدیمی")
    character_a = await _make_character(db_session, code="main_stale_a")
    character_b = await _make_character(db_session, code="main_stale_b")
    card_a = await PlayerCardService.acquire(db_session, user.id, character_a.id)
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    user_id, card_a_id, card_b_id = user.id, card_a.id, card_b.id
    await db_session.commit()

    await PlayerCardService.set_favorite(
        db_session, user_id, card_a_id, expected_main_card_id=0
    )
    await db_session.commit()

    with pytest.raises(StaleMainCardSelectionError):
        await PlayerCardService.set_favorite(
            db_session, user_id, card_b_id, expected_main_card_id=0
        )
    await db_session.rollback()

    assert (await PlayerCardService.get_main_card(db_session, user_id)).id == card_a_id
    card_b = await db_session.get(PlayerCard, card_b_id)
    assert not card_b.is_favorite


@pytest.mark.asyncio
async def test_reselect_repairs_legacy_duplicate_favorites_deterministically(db_session):
    user = await create_test_user(db_session, "کارت‌باز وضعیت قدیمی")
    character_a = await _make_character(db_session, code="main_legacy_a")
    character_b = await _make_character(db_session, code="main_legacy_b")
    card_a = await PlayerCardService.acquire(db_session, user.id, character_a.id)
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    card_a.is_favorite = True
    card_b.is_favorite = True
    await db_session.commit()

    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == min(
        card_a.id, card_b.id
    )
    main_card = await PlayerCardService.get_main_card(db_session, user.id)
    await PlayerCardService.set_favorite(
        db_session, user.id, main_card.id, expected_main_card_id=main_card.id
    )
    await db_session.commit()

    cards = await PlayerCardService.list_cards(db_session, user.id)
    assert [card.id for card in cards if card.is_favorite] == [main_card.id]


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_first_main_card_selections_are_serialized_by_user_row(db_session):
    """A row-lock-capable database permits only one competing initial choice."""
    user = await create_test_user(db_session, "کارت‌باز همزمان")
    character_a = await _make_character(db_session, code="main_concurrent_a")
    character_b = await _make_character(db_session, code="main_concurrent_b")
    card_a = await PlayerCardService.acquire(db_session, user.id, character_a.id)
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    await db_session.commit()

    factory = get_async_sessionmaker()

    async def select_main(card_id: int) -> str:
        async with factory() as session:
            try:
                await PlayerCardService.set_favorite(
                    session, user.id, card_id, expected_main_card_id=0
                )
                await session.commit()
                return "selected"
            except StaleMainCardSelectionError:
                await session.rollback()
                return "stale"

    results = await asyncio.gather(select_main(card_a.id), select_main(card_b.id))
    assert sorted(results) == ["selected", "stale"]

    async with factory() as session:
        main_card = await PlayerCardService.get_main_card(session, user.id)
        cards = await PlayerCardService.list_cards(session, user.id)
        assert main_card is not None
        assert main_card.id in {card_a.id, card_b.id}
        assert [card.id for card in cards if card.is_favorite] == [main_card.id]


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_main_card_switches_reject_the_losing_stale_view(db_session):
    """Two different switches based on one rendered Main Card cannot both win."""
    user = await create_test_user(db_session, "کارت‌باز تغییر همزمان")
    character_a = await _make_character(db_session, code="main_switch_a")
    character_b = await _make_character(db_session, code="main_switch_b")
    character_c = await _make_character(db_session, code="main_switch_c")
    card_a = await PlayerCardService.acquire(db_session, user.id, character_a.id)
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    card_c = await PlayerCardService.acquire(db_session, user.id, character_c.id)
    user_id, current_id = user.id, card_a.id
    await db_session.commit()

    await PlayerCardService.set_favorite(
        db_session, user_id, current_id, expected_main_card_id=0
    )
    await db_session.commit()
    factory = get_async_sessionmaker()

    async def switch_main(card_id: int) -> str:
        async with factory() as session:
            try:
                await PlayerCardService.set_favorite(
                    session,
                    user_id,
                    card_id,
                    expected_main_card_id=current_id,
                )
                await session.commit()
                return "selected"
            except StaleMainCardSelectionError:
                await session.rollback()
                return "stale"

    results = await asyncio.gather(
        switch_main(card_b.id),
        switch_main(card_c.id),
    )
    assert sorted(results) == ["selected", "stale"]

    async with factory() as session:
        main_card = await PlayerCardService.get_main_card(session, user_id)
        cards = await PlayerCardService.list_cards(session, user_id)
        assert main_card is not None
        assert main_card.id in {card_b.id, card_c.id}
        assert [card.id for card in cards if card.is_favorite] == [main_card.id]


