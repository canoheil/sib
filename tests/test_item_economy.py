"""Item economy: buying with Apples, selling back, config-driven prices."""

import pytest

from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.tests.test_parties import create_test_user


async def _funded_user(session, apples: int = 1000):
    user = await create_test_user(session, "خریدار تست")
    await AppleWalletService.earn(session, user.id, apples, reason="test_grant")
    await session.commit()
    return user  # NB: do not touch user.* after a rollback (expired).


# ---------------------------------------------------------------------------
# Buy (Apples -> items), spec: shop Items section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_charges_apples_and_grants_item(db_session):
    user = await _funded_user(db_session, 100)

    total = await ItemService.buy(db_session, user.id, "magic_powder", 1)
    await db_session.commit()

    price = await ItemService.buy_price(db_session, "magic_powder")
    assert price == 40
    assert total == 1
    assert await ItemService.get_total(db_session, user.id, "magic_powder") == 1
    assert await AppleWalletService.get_balance(db_session, user.id) == 100 - price


@pytest.mark.asyncio
async def test_buy_is_repeatable_and_scales_with_quantity(db_session):
    """Every press is a real paid transaction (no daily limit)."""
    user = await _funded_user(db_session, 500)

    await ItemService.buy(db_session, user.id, "magic_powder", 2)
    total = await ItemService.buy(db_session, user.id, "magic_powder", 3)
    await db_session.commit()

    assert total == 5
    assert await ItemService.get_total(db_session, user.id, "magic_powder") == 5
    assert await AppleWalletService.get_balance(db_session, user.id) == 500 - 40 * 5


@pytest.mark.asyncio
async def test_buy_rejected_without_apples(db_session):
    user = await _funded_user(db_session, 10)  # price is 40
    uid = user.id

    with pytest.raises(ValueError):
        await ItemService.buy(db_session, uid, "magic_powder")
    await db_session.rollback()
    assert await ItemService.get_total(db_session, uid, "magic_powder") == 0
    assert await AppleWalletService.get_balance(db_session, uid) == 10


@pytest.mark.asyncio
async def test_buy_rejected_for_unpriced_item(db_session):
    """Only items with a configured buy price are purchasable."""
    user = await _funded_user(db_session, 1000)
    uid = user.id

    with pytest.raises(ValueError):
        await ItemService.buy(db_session, uid, "apple_obsidian")  # sell-only
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Sell (items -> Apples), spec: shop Sell Items section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_credits_configured_price(db_session):
    user = await _funded_user(db_session, 0)
    await ItemService.grant(db_session, user.id, "apple_obsidian", 1)
    await db_session.commit()

    credited = await ItemService.sell(db_session, user.id, "apple_obsidian")
    await db_session.commit()

    assert credited == 200  # config: item_sell_prices
    assert await ItemService.get_total(db_session, user.id, "apple_obsidian") == 0
    assert await AppleWalletService.get_balance(db_session, user.id) == 200


@pytest.mark.asyncio
async def test_sell_rejects_short_inventory(db_session):
    user = await _funded_user(db_session, 0)
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 1)
    await db_session.commit()

    with pytest.raises(ValueError):
        await ItemService.sell(db_session, uid, "apple_gold", 2)
    await db_session.rollback()
    assert await ItemService.get_total(db_session, uid, "apple_gold") == 1
    assert await AppleWalletService.get_balance(db_session, uid) == 0


@pytest.mark.asyncio
async def test_sell_rejects_unsellable_item(db_session):
    """Magic Powder is consumable progression, not sellable currency."""
    user = await _funded_user(db_session, 0)
    uid = user.id
    await ItemService.grant(db_session, uid, "magic_powder", 5)
    await db_session.commit()

    with pytest.raises(ValueError):
        await ItemService.sell(db_session, uid, "magic_powder")
    await db_session.rollback()


@pytest.mark.asyncio
async def test_prices_are_pure_config(db_session):
    """Rebalance = one config row, no code change (spec #12)."""
    await ConfigService.set(db_session, "item_buy_prices", {"magic_powder": 7})
    user = await _funded_user(db_session, 7)

    await ItemService.buy(db_session, user.id, "magic_powder", 1)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, user.id) == 0

    # Restore the shared config default for other tests in this session.
    await ConfigService.set(db_session, "item_buy_prices", {"magic_powder": 40})
    await db_session.commit()
