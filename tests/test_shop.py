"""Tests for the weekly rotating shop (Phase 11)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sib.database.models import ReleaseStatus, ShopEntry, ShopRotation
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.config_store import ConfigService
from sib.services.player_cards import PlayerCardService
from sib.services.shop import ShopError, ShopService
from sib.services.player_xp import PlayerXPService
from sib.tests.test_parties import create_test_user


async def _make_characters(db_session, count=4):
    """Create released COMMON cards, priced by rarity rather than legacy price."""
    chars = []
    for i in range(count):
        character = await CharacterService.create(
            db_session,
            code=f"shop_char_{i}",
            name_fa=f"قهرمان فروشگاه {i}",
            power=20,
            speed=20,
            intelligence=20,
        )
        await CharacterService.set_release_status(
            db_session, character.id, ReleaseStatus.RELEASED
        )
        chars.append(character)
    await db_session.commit()
    return chars


@pytest.mark.asyncio
async def test_rotation_created_on_demand_with_entries(db_session):
    chars = await _make_characters(db_session, 4)

    rotation = await ShopService.current_rotation(db_session)
    await db_session.commit()

    assert rotation.is_active
    assert rotation.ends_at > rotation.starts_at

    entries = await ShopService.list_entries(db_session)
    assert len(entries) == 4
    assert {c.id for _, c, _ in entries} == {c.id for c in chars}
    # Badges are Persian.
    assert entries[0][0].badge == "🔥 محبوب این هفته"


@pytest.mark.asyncio
async def test_rotation_reuse_and_rotation_policy(db_session):
    chars = await _make_characters(db_session, 3)

    rotation1 = await ShopService.current_rotation(db_session)
    await db_session.commit()
    # Same window is reused.
    rotation2 = await ShopService.current_rotation(db_session)
    assert rotation1.id == rotation2.id

    # Expire the window → a new weighted selection opens.
    rotation1.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    rotation3 = await ShopService.current_rotation(db_session)
    await db_session.commit()
    assert rotation3.id != rotation1.id

    entries = await ShopService.list_entries(db_session)
    assert len(entries) == 3
    assert {c.id for _, c, _ in entries} == {c.id for c in chars}

    # Small pools still offer every eligible card (re-entry allowed).
    rotation3.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    await ShopService.current_rotation(db_session)
    await db_session.commit()
    entries = await ShopService.list_entries(db_session)
    assert {c.id for _, c, _ in entries} == {c.id for c in chars}


@pytest.mark.asyncio
async def test_purchase_deducts_apples_and_grants_card(db_session):
    chars = await _make_characters(db_session, 2)
    user = await create_test_user(db_session, "مشتری ۱")
    await AppleWalletService.earn(db_session, user.id, 500, reason="test_grant")
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    entry, character, owned = entries[0]
    assert not owned

    price = ShopService.card_price(character)
    outcome = await ShopService.purchase(db_session, user.id, entry.id, nonce="buytest0001")
    card = outcome.card
    await db_session.commit()

    assert card.character_id == character.id
    assert outcome.price == price
    assert await AppleWalletService.get_balance(db_session, user.id) == 500 - price
    assert (await PlayerXPService.get_progress(db_session, user.id)).total_xp == 25

    # Now marked as owned in listings.
    _, _, owned = (await ShopService.list_entries(db_session, user_id=user.id))[0]
    assert owned


@pytest.mark.asyncio
async def test_purchase_requires_enough_apples(db_session):
    chars = await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "مشتری ۲")
    await AppleWalletService.earn(db_session, user.id, 10, reason="test_grant")
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user.id, entries[0][0].id, nonce="buytest0002")

    # No card and no charge.
    cards = await PlayerCardService.list_cards(db_session, user.id)
    assert cards == []
    assert await AppleWalletService.get_balance(db_session, user.id) == 10


@pytest.mark.asyncio
async def test_duplicate_purchase_blocked_and_not_charged_twice(db_session):
    chars = await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "مشتری ۳")
    await AppleWalletService.earn(db_session, user.id, 500, reason="test_grant")
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    entry = entries[0][0]
    await ShopService.purchase(db_session, user.id, entry.id, nonce="dupfirst0001")
    await db_session.commit()
    balance_after_first = await AppleWalletService.get_balance(db_session, user.id)

    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user.id, entry.id, nonce="dupsecond01")

    assert await AppleWalletService.get_balance(db_session, user.id) == balance_after_first
    cards = await PlayerCardService.list_cards(db_session, user.id)
    assert len(cards) == 1


@pytest.mark.asyncio
async def test_expired_rotation_cannot_be_purchased(db_session):
    chars = await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "مشتری ۴")
    await AppleWalletService.earn(db_session, user.id, 500, reason="test_grant")
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    entry = entries[0][0]

    rotation = await db_session.get(ShopRotation, entry.rotation_id)
    rotation.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user.id, entry.id, nonce="expired0001")


@pytest.mark.asyncio
async def test_pool_config_limits_entries(db_session):
    chars = await _make_characters(db_session, 4)
    await ConfigService.set(
        db_session,
        "shop_pool_character_ids",
        [chars[1].id, chars[3].id],
    )
    await db_session.commit()

    rotation = await ShopService.current_rotation(db_session)
    entries = await ShopService.list_entries(db_session)
    assert {c.id for _, c, _ in entries} == {chars[1].id, chars[3].id}


@pytest.mark.parametrize("pool_size", [5, 6, 21])
async def test_rotation_selects_exactly_five_unique_cards_and_reuses_them(db_session, pool_size):
    chars = await _make_characters(db_session, pool_size)
    entries = await ShopService.list_entries(db_session)
    selected = [(entry.id, character.id) for entry, character, _ in entries]
    assert len(selected) == 5
    assert len({character_id for _, character_id in selected}) == 5
    assert {character_id for _, character_id in selected} <= {c.id for c in chars}
    assert [entry.sort_order for entry, _, _ in entries] == list(range(5))
    await db_session.commit()
    assert [(e.id, c.id) for e, c, _ in await ShopService.list_entries(db_session)] == selected

    rotation = await ShopService.current_rotation(db_session)
    rotation.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    next_entries = await ShopService.list_entries(db_session)
    assert len(next_entries) == 5
    assert len({character.id for _, character, _ in next_entries}) == 5
    historical = list((await db_session.scalars(
        select(ShopEntry).where(ShopEntry.rotation_id == rotation.id).order_by(ShopEntry.sort_order)
    )).all())
    assert [(entry.id, entry.character_id) for entry in historical] == selected


async def test_five_card_selection_respects_configured_pool_and_eligibility(db_session):
    chars = await _make_characters(db_session, 12)
    chars[0].is_active = False
    chars[1].shop_enabled = False
    chars[2].release_status = ReleaseStatus.DRAFT
    chars[3].release_status = ReleaseStatus.RETIRED
    await ConfigService.set(db_session, "shop_pool_character_ids", [c.id for c in chars[:10]])
    entries = await ShopService.list_entries(db_session)
    assert len(entries) == 5
    assert {c.id for _, c, _ in entries} <= {c.id for c in chars[4:10]}


def test_weighted_selection_makes_legendaries_rare_but_possible():
    from collections import Counter

    from sib.database.models import Character

    pool = [
        Character(id=index * 8 + offset, rarity=rarity)
        for index, rarity in enumerate(("COMMON", "RARE", "EPIC", "LEGENDARY"))
        for offset in range(8)
    ]
    counts = Counter()
    for sequence in range(1, 2001):
        selected = ShopService._select_characters(pool, sequence)
        assert len(selected) == len({c.id for c in selected}) == 5
        counts.update(c.rarity for c in selected)
    assert counts["COMMON"] > counts["RARE"] > counts["EPIC"] > counts["LEGENDARY"] > 0
    assert counts["LEGENDARY"] < 0.02 * sum(counts.values())
    assert ShopService._select_characters(pool, 42) == ShopService._select_characters(pool, 42)
    assert len(pool) == 32  # Sampling does not mutate the pool.


def test_empty_rotation_pool_has_no_entries():
    assert ShopService._select_characters([], 1) == []
