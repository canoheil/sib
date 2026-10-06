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
    chars = []
    for i in range(count):
        character = await CharacterService.create(
            db_session,
            code=f"shop_char_{i}",
            name_fa=f"قهرمان فروشگاه {i}",
            power=20,
            speed=20,
            intelligence=20,
            price=100 + i * 50,
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
    # Round-robin starts at the beginning of the pool.
    assert entries[0][1].id == chars[0].id
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

    # Expire the window → a new rotation opens with shifted entries.
    rotation1.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    rotation3 = await ShopService.current_rotation(db_session)
    await db_session.commit()
    assert rotation3.id != rotation1.id

    entries = await ShopService.list_entries(db_session)
    # Round-robin offset moved by one → first entry is the second character.
    assert entries[0][1].id == chars[1].id

    # After a full cycle, every character re-enters (re-entry allowed).
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

    price = ShopService.entry_price(entry, character)
    card = await ShopService.purchase(db_session, user.id, entry.id)
    await db_session.commit()

    assert card.character_id == character.id
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
        await ShopService.purchase(db_session, user.id, entries[0][0].id)

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
    await ShopService.purchase(db_session, user.id, entry.id)
    await db_session.commit()
    balance_after_first = await AppleWalletService.get_balance(db_session, user.id)

    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user.id, entry.id)

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
        await ShopService.purchase(db_session, user.id, entry.id)


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
