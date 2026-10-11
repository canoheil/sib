"""Purchase-time eligibility for shop cards (remediation of the audit).

A character that was released, shop-enabled and active when the rotation was
created may be retired, shop-disabled or deactivated afterwards. The button
for it can still be on screen, so the purchase itself must refuse, without
writing a card or a charge. An already-paid nonce still replays, because the
replay check runs before the eligibility check.
"""

import pytest
from sqlalchemy import func, select

from sib.database.models import Character, PlayerCard, ReleaseStatus
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.shop import ShopError, ShopService
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters


async def _setup_buyer(db_session, *, apples: int = 1000):
    chars = await _make_characters(db_session, 2)
    user = await create_test_user(db_session, "مشتری صلاحیت")
    await AppleWalletService.earn(db_session, user.id, apples, reason="test_grant")
    await db_session.commit()
    entries = await ShopService.list_entries(db_session)
    entry, character, _owned = entries[0]
    return user.id, entry.id, character.id, chars


async def _card_count(db_session, user_id: int, character_id: int) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(PlayerCard)
        .where(PlayerCard.user_id == user_id, PlayerCard.character_id == character_id)
    )


@pytest.mark.asyncio
async def test_character_retired_after_rotation_is_refused(db_session):
    user_id, entry_id, character_id, _ = await _setup_buyer(db_session)
    balance_before = await AppleWalletService.get_balance(db_session, user_id)

    await CharacterService.set_release_status(
        db_session, character_id, ReleaseStatus.RETIRED
    )
    await db_session.commit()

    with pytest.raises(ShopError):
        await ShopService.purchase(
            db_session, user_id, entry_id, nonce="retired0001"
        )
    await db_session.rollback()

    assert await _card_count(db_session, user_id, character_id) == 0
    assert await AppleWalletService.get_balance(db_session, user_id) == balance_before


@pytest.mark.asyncio
async def test_character_shop_disabled_after_rotation_is_refused(db_session):
    user_id, entry_id, character_id, _ = await _setup_buyer(db_session)
    balance_before = await AppleWalletService.get_balance(db_session, user_id)

    character = await db_session.get(Character, character_id)
    character.shop_enabled = False
    await db_session.commit()

    with pytest.raises(ShopError):
        await ShopService.purchase(
            db_session, user_id, entry_id, nonce="disabled0001"
        )
    await db_session.rollback()

    assert await _card_count(db_session, user_id, character_id) == 0
    assert await AppleWalletService.get_balance(db_session, user_id) == balance_before


@pytest.mark.asyncio
async def test_character_deactivated_after_rotation_is_refused(db_session):
    user_id, entry_id, character_id, _ = await _setup_buyer(db_session)
    balance_before = await AppleWalletService.get_balance(db_session, user_id)

    character = await db_session.get(Character, character_id)
    character.is_active = False
    await db_session.commit()

    with pytest.raises(ShopError):
        await ShopService.purchase(
            db_session, user_id, entry_id, nonce="inactive0001"
        )
    await db_session.rollback()

    assert await _card_count(db_session, user_id, character_id) == 0
    assert await AppleWalletService.get_balance(db_session, user_id) == balance_before


@pytest.mark.asyncio
async def test_draft_character_is_refused(db_session):
    """Only RELEASED counts: a character moved back to DRAFT is not sold."""
    user_id, entry_id, character_id, _ = await _setup_buyer(db_session)
    await CharacterService.set_release_status(
        db_session, character_id, ReleaseStatus.DRAFT
    )
    await db_session.commit()

    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="draft00001")
    await db_session.rollback()
    assert await _card_count(db_session, user_id, character_id) == 0


@pytest.mark.asyncio
async def test_paid_nonce_still_replays_after_character_is_retired(db_session):
    """The replay check precedes eligibility: a paid press is answered from the
    ledger (no charge, no card) even though the character is now retired."""
    user_id, entry_id, character_id, _ = await _setup_buyer(db_session)

    first = await ShopService.purchase(db_session, user_id, entry_id, nonce="paidfirst01")
    await db_session.commit()
    assert first.replayed is False
    balance_after_first = await AppleWalletService.get_balance(db_session, user_id)

    await CharacterService.set_release_status(
        db_session, character_id, ReleaseStatus.RETIRED
    )
    await db_session.commit()

    replay = await ShopService.purchase(db_session, user_id, entry_id, nonce="paidfirst01")
    await db_session.commit()
    assert replay.replayed is True
    assert await AppleWalletService.get_balance(db_session, user_id) == balance_after_first
    assert await _card_count(db_session, user_id, character_id) == 1


@pytest.mark.asyncio
async def test_new_nonce_after_retirement_is_not_a_new_purchase(db_session):
    """A fresh nonce on a retired character is a new purchase attempt: refused."""
    user_id, entry_id, character_id, _ = await _setup_buyer(db_session)
    await ShopService.purchase(db_session, user_id, entry_id, nonce="paidonce001")
    await db_session.commit()
    await CharacterService.set_release_status(
        db_session, character_id, ReleaseStatus.RETIRED
    )
    await db_session.commit()
    balance = await AppleWalletService.get_balance(db_session, user_id)

    # A different nonce must not buy again (and the card is already owned).
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="newnonce002")
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, user_id) == balance
