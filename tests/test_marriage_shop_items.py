"""Phase 7.5 — Marriage items (💍 Ring / 💐 Flower) in the Shop economy.

The Shop was NOT redesigned. ``handlers/shop.py`` builds both the "Items"
and the "Sell Items" screens straight out of the ``item_buy_prices`` /
``item_sell_prices`` config maps (ordered by ``ITEM_CATALOG``), so wiring
these two items into the economy is pure configuration:

    CONFIG_DEFAULTS["item_buy_prices"]["wedding_ring"] = 1000
    CONFIG_DEFAULTS["item_buy_prices"]["flower"]       = 30
    CONFIG_DEFAULTS["item_sell_prices"]["wedding_ring"] = 500   # 1000 * 0.5
    CONFIG_DEFAULTS["item_sell_prices"]["flower"]       = 15    #   30 * 0.5

No handler and no service needed a code change; the tests below prove the
prices are read from config rather than hardcoded anywhere, and that the
marriage/Love domains keep recognising the very same ``UserItem`` rows the
shop sells.

This module is SHOP/ECONOMY only. The Telegram flower send/accept/reject
UI, Love decay and every marriage/Love domain rule are out of scope.
"""

import pytest

from sib.database import Group, Marriage, MarriageStatus, ProposalStatus, User
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.items import ITEM_CATALOG, ItemService, item_emoji, item_label
from sib.services.love import FlowerRequiredError, LoveService
from sib.services.marriage import MarriageService, WeddingRingMissingAtAcceptanceError
from sib.services.marriage_proposal import (
    MarriageProposal,
    MarriageProposalService,
    WeddingRingRequiredError,
)

GROUP_ID = -1009600

RING_BUY = 1000
RING_SELL = 500
FLOWER_BUY = 30
FLOWER_SELL = 15

SHOPPER_ID = 9600
SPOUSE_ID = 9601
GROUPSMATE_ID = 9602


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _mk_user(session, user_id: int, name: str = "کاربر") -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=f"{name}{user_id}",
            language_code="fa",
            is_bot=False,
        )
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = GROUP_ID) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title="گروه فروشگاه سیب")
        session.add(group)
        await session.flush()
    return group


async def _funded(session, user_id: int, apples: int) -> User:
    """A user with exactly ``apples`` normal Apples in the wallet."""
    user = await _mk_user(session, user_id)
    if apples:
        await AppleWalletService.earn(session, user_id, apples, reason="test_grant")
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# 1. Pricing is configuration, never hardcoded
# ---------------------------------------------------------------------------


def test_marriage_items_are_in_the_catalog():
    """Both items already have catalog entries (emoji/label/description)."""
    for key in ("wedding_ring", "flower"):
        assert key in ITEM_CATALOG
        assert item_emoji(key)
        assert item_label(key) != key
    assert item_emoji("wedding_ring") == "💍"
    assert item_label("wedding_ring") == "حلقه ازدواج"
    assert item_emoji("flower") == "💐"
    assert item_label("flower") == "گل"


def test_ring_and_flower_have_configured_buy_prices():
    buys = CONFIG_DEFAULTS["item_buy_prices"]
    assert buys["wedding_ring"] == RING_BUY
    assert buys["flower"] == FLOWER_BUY


def test_ring_and_flower_sell_prices_follow_the_configured_ratio():
    """Sell = buy * ``item_sell_price_ratio`` — derived, not invented."""
    ratio = CONFIG_DEFAULTS["item_sell_price_ratio"]
    buys = CONFIG_DEFAULTS["item_buy_prices"]
    sells = CONFIG_DEFAULTS["item_sell_prices"]
    assert ratio == 0.5
    for key in ("wedding_ring", "flower"):
        assert sells[key] == int(buys[key] * ratio)
    assert sells["wedding_ring"] == RING_SELL
    assert sells["flower"] == FLOWER_SELL


@pytest.mark.asyncio
async def test_prices_are_read_from_config_not_hardcoded(db_session):
    """Override the config row -> the service must follow it immediately."""
    await _funded(db_session, SHOPPER_ID, 5000)
    await db_session.commit()

    assert await ItemService.buy_price(db_session, "flower") == FLOWER_BUY

    await ConfigService.set(
        db_session,
        "item_buy_prices",
        {"magic_powder": 40, "wedding_ring": 12, "flower": 5},
    )
    await ConfigService.set(
        db_session,
        "item_sell_prices",
        {"apple_gold": 50, "wedding_ring": 3, "flower": 1},
    )
    await db_session.commit()

    assert await ItemService.buy_price(db_session, "flower") == 5
    assert await ItemService.buy_price(db_session, "wedding_ring") == 12
    assert await ItemService.sell_price(db_session, "flower") == 1
    assert await ItemService.sell_price(db_session, "wedding_ring") == 3

    await ItemService.buy(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 5000 - 5


@pytest.mark.asyncio
async def test_shop_lists_both_marriage_items_in_catalog_order(db_session):
    """The Shop's own ordering helper exposes them with no handler change."""
    from sib.handlers.shop import _buyable_keys

    buy_prices = await ConfigService.get(db_session, "item_buy_prices")
    keys = _buyable_keys(buy_prices)

    assert keys == ["magic_powder", "sib_buff", "wedding_ring", "flower"]
    assert "wedding_ring" in keys and "flower" in keys


@pytest.mark.asyncio
async def test_sellable_inventory_offers_both_marriage_items_when_owned(db_session):
    await _funded(db_session, SHOPPER_ID, 0)
    await ItemService.grant(db_session, SHOPPER_ID, "wedding_ring", 1)
    await ItemService.grant(db_session, SHOPPER_ID, "flower", 2)
    await db_session.commit()

    sellable = dict(
        (key, (qty, price))
        for key, qty, price in await ItemService.sellable_inventory(
            db_session, SHOPPER_ID
        )
    )
    assert sellable["wedding_ring"] == (1, RING_SELL)
    assert sellable["flower"] == (2, FLOWER_SELL)


# ---------------------------------------------------------------------------
# 2. Wedding Ring: buy / sell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_ring(db_session):
    await _funded(db_session, SHOPPER_ID, RING_BUY)
    await db_session.commit()

    total = await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()

    assert total == 1
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 1
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 0


@pytest.mark.asyncio
async def test_buy_ring_charges_the_configured_price(db_session):
    await _funded(db_session, SHOPPER_ID, RING_BUY + 250)
    await db_session.commit()

    price = await ItemService.buy_price(db_session, "wedding_ring")
    assert price == RING_BUY

    await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 250


@pytest.mark.asyncio
async def test_sell_ring(db_session):
    await _funded(db_session, SHOPPER_ID, 0)
    await ItemService.grant(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()

    credited = await ItemService.sell(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()

    assert credited == RING_SELL
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 0
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == RING_SELL


@pytest.mark.asyncio
async def test_sell_ring_credits_the_configured_price(db_session):
    await _funded(db_session, SHOPPER_ID, 0)
    await ItemService.grant(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()

    price = await ItemService.sell_price(db_session, "wedding_ring")
    assert price == RING_SELL
    assert price == int(RING_BUY * CONFIG_DEFAULTS["item_sell_price_ratio"])


@pytest.mark.asyncio
async def test_buy_ring_with_insufficient_balance(db_session):
    await _funded(db_session, SHOPPER_ID, RING_BUY - 1)
    await db_session.commit()

    with pytest.raises(ValueError):
        await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.rollback()

    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 0
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == RING_BUY - 1


@pytest.mark.asyncio
async def test_ring_inventory_increases_and_decreases(db_session):
    await _funded(db_session, SHOPPER_ID, RING_BUY * 3)
    await db_session.commit()

    await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 2)
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 2

    await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 3
    await db_session.commit()

    credited = await ItemService.sell(db_session, SHOPPER_ID, "wedding_ring", 2)
    await db_session.commit()

    assert credited == RING_SELL * 2
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 1
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == RING_SELL * 2


# ---------------------------------------------------------------------------
# 3. Flower: buy / sell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_flower(db_session):
    await _funded(db_session, SHOPPER_ID, FLOWER_BUY)
    await db_session.commit()

    total = await ItemService.buy(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()

    assert total == 1
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 1
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 0


@pytest.mark.asyncio
async def test_buy_flower_charges_the_configured_price(db_session):
    await _funded(db_session, SHOPPER_ID, FLOWER_BUY + 70)
    await db_session.commit()

    price = await ItemService.buy_price(db_session, "flower")
    assert price == FLOWER_BUY

    await ItemService.buy(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 70


@pytest.mark.asyncio
async def test_sell_flower(db_session):
    await _funded(db_session, SHOPPER_ID, 0)
    await ItemService.grant(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()

    credited = await ItemService.sell(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()

    assert credited == FLOWER_SELL
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 0
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == FLOWER_SELL


@pytest.mark.asyncio
async def test_sell_flower_credits_the_configured_price(db_session):
    await _funded(db_session, SHOPPER_ID, 0)
    await ItemService.grant(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()

    price = await ItemService.sell_price(db_session, "flower")
    assert price == FLOWER_SELL
    assert price == int(FLOWER_BUY * CONFIG_DEFAULTS["item_sell_price_ratio"])


@pytest.mark.asyncio
async def test_buy_flower_with_insufficient_balance(db_session):
    await _funded(db_session, SHOPPER_ID, FLOWER_BUY - 1)
    await db_session.commit()

    with pytest.raises(ValueError):
        await ItemService.buy(db_session, SHOPPER_ID, "flower", 1)
    await db_session.rollback()

    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 0
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == FLOWER_BUY - 1


@pytest.mark.asyncio
async def test_flower_inventory_increases_and_decreases(db_session):
    await _funded(db_session, SHOPPER_ID, FLOWER_BUY * 5)
    await db_session.commit()

    await ItemService.buy(db_session, SHOPPER_ID, "flower", 3)
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 3

    await ItemService.buy(db_session, SHOPPER_ID, "flower", 2)
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 5
    await db_session.commit()

    credited = await ItemService.sell(db_session, SHOPPER_ID, "flower", 4)
    await db_session.commit()

    assert credited == FLOWER_SELL * 4
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 1
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == (
        FLOWER_SELL * 4
    )


@pytest.mark.asyncio
async def test_selling_more_than_owned_is_refused(db_session):
    await _funded(db_session, SHOPPER_ID, 0)
    await ItemService.grant(db_session, SHOPPER_ID, "flower", 1)
    await db_session.commit()

    with pytest.raises(ValueError):
        await ItemService.sell(db_session, SHOPPER_ID, "flower", 2)
    await db_session.rollback()

    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 1
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 0


# ---------------------------------------------------------------------------
# 4. Integration with the marriage / Love domains
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_bought_in_the_shop_is_recognised_by_the_proposal_service(db_session):
    """A shop-bought Ring satisfies the proposal ownership check."""
    await _funded(db_session, SHOPPER_ID, RING_BUY * 2)
    await _mk_user(db_session, SPOUSE_ID)
    await _mk_group(db_session)
    await db_session.commit()

    # Without a Ring the proposal is refused outright.
    with pytest.raises(WeddingRingRequiredError):
        await MarriageProposalService.create_proposal(
            db_session, SHOPPER_ID, SPOUSE_ID, GROUP_ID
        )
    await db_session.rollback()

    # Buy one in the shop, then the very same row is accepted by the domain.
    await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, SHOPPER_ID, SPOUSE_ID, GROUP_ID
    )
    await db_session.commit()

    assert proposal.status == ProposalStatus.PENDING
    # Creation does NOT consume the Ring.
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_selling_the_ring_after_creating_a_proposal_blocks_acceptance(db_session):
    """The Ring may be sold while a proposal is pending; acceptance then
    fails cleanly instead of consuming or inventing a Ring."""
    await _funded(db_session, SHOPPER_ID, RING_BUY)
    await _mk_user(db_session, SPOUSE_ID)
    await _mk_group(db_session)
    await db_session.commit()

    total = await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    assert total == 1
    proposal = await MarriageProposalService.create_proposal(
        db_session, SHOPPER_ID, SPOUSE_ID, GROUP_ID
    )
    await db_session.commit()
    proposal_id = int(proposal.id)

    # Sell the Ring back while the proposal is still PENDING.
    credited = await ItemService.sell(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()
    assert credited == RING_SELL
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 0

    # Acceptance is refused — no phantom Ring is consumed or refunded.
    with pytest.raises(WeddingRingMissingAtAcceptanceError):
        await MarriageService.accept_proposal(db_session, proposal_id, SPOUSE_ID)
    await db_session.rollback()

    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 0
    reloaded = await db_session.get(MarriageProposal, proposal_id)
    assert reloaded.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_successful_acceptance_consumes_exactly_one_ring(db_session):
    await _funded(db_session, SHOPPER_ID, RING_BUY * 3)
    await _mk_user(db_session, SPOUSE_ID)
    await _mk_group(db_session)
    await db_session.commit()

    await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 2)
    proposal = await MarriageProposalService.create_proposal(
        db_session, SHOPPER_ID, SPOUSE_ID, GROUP_ID
    )
    await db_session.commit()

    await MarriageService.accept_proposal(db_session, int(proposal.id), SPOUSE_ID)
    await db_session.commit()

    # Exactly one Ring consumed (2 -> 1); the spare is untouched and saleable.
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 1
    credited = await ItemService.sell(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()
    assert credited == RING_SELL


@pytest.mark.asyncio
async def test_rejected_proposal_keeps_the_ring_saleable(db_session):
    await _funded(db_session, SHOPPER_ID, RING_BUY)
    await _mk_user(db_session, SPOUSE_ID)
    await _mk_group(db_session)
    await db_session.commit()

    await ItemService.buy(db_session, SHOPPER_ID, "wedding_ring", 1)
    proposal = await MarriageProposalService.create_proposal(
        db_session, SHOPPER_ID, SPOUSE_ID, GROUP_ID
    )
    await db_session.commit()

    await MarriageProposalService.reject_proposal(
        db_session, int(proposal.id), SPOUSE_ID
    )
    await db_session.commit()

    # A rejected proposal costs nothing: the Ring is still there to sell.
    assert await ItemService.get_total(db_session, SHOPPER_ID, "wedding_ring") == 1
    credited = await ItemService.sell(db_session, SHOPPER_ID, "wedding_ring", 1)
    await db_session.commit()
    assert credited == RING_SELL


@pytest.mark.asyncio
async def test_flower_bought_in_the_shop_is_recognised_by_love_service(db_session):
    """A shop-bought Flower is the item LoveService consumes on send."""
    await _funded(db_session, SHOPPER_ID, FLOWER_BUY * 2)
    await _mk_user(db_session, SPOUSE_ID)
    await _mk_group(db_session)
    await db_session.commit()

    await ItemService.grant(db_session, SHOPPER_ID, "wedding_ring", 1)
    marriage = Marriage(
        proposer_id=SHOPPER_ID,
        accepter_id=SPOUSE_ID,
        origin_group_id=GROUP_ID,
        status=MarriageStatus.ACTIVE,
        love_score=10,
    )
    db_session.add(marriage)
    await ConfigService.set(db_session, "flower_love_gain", 5)
    await db_session.commit()

    with pytest.raises(FlowerRequiredError):
        await LoveService.send_flower(
            db_session,
            sender_user_id=SHOPPER_ID,
            receiver_user_id=SPOUSE_ID,
            chat_id=GROUP_ID,
            message_id=1,
        )
    await db_session.rollback()

    await ItemService.buy(db_session, SHOPPER_ID, "flower", 2)
    await db_session.commit()

    result = await LoveService.send_flower(
        db_session,
        sender_user_id=SHOPPER_ID,
        receiver_user_id=SPOUSE_ID,
        chat_id=GROUP_ID,
        message_id=2,
    )
    await db_session.commit()

    assert result.flower_consumed is True
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_unsent_flower_can_be_sold_back(db_session):
    """Flowers are ordinary inventory until LoveService consumes one."""
    await _funded(db_session, SHOPPER_ID, FLOWER_BUY * 2)
    await db_session.commit()

    await ItemService.buy(db_session, SHOPPER_ID, "flower", 2)
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 2

    credited = await ItemService.sell(db_session, SHOPPER_ID, "flower", 2)
    await db_session.commit()

    assert credited == FLOWER_SELL * 2
    assert await ItemService.get_total(db_session, SHOPPER_ID, "flower") == 0


# ---------------------------------------------------------------------------
# 5. Magic Powder is untouched (scope guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_powder_pricing_and_behaviour_are_unchanged(db_session):
    await _funded(db_session, SHOPPER_ID, 100)
    await db_session.commit()

    assert await ItemService.buy_price(db_session, "magic_powder") == 40

    total = await ItemService.buy(db_session, SHOPPER_ID, "magic_powder", 1)
    await db_session.commit()
    assert total == 1
    assert await AppleWalletService.get_balance(db_session, SHOPPER_ID) == 60

    # Still deliberately NOT sellable.
    assert await ItemService.sell_price(db_session, "magic_powder") is None
    with pytest.raises(ValueError):
        await ItemService.sell(db_session, SHOPPER_ID, "magic_powder", 1)
    await db_session.rollback()
    assert await ItemService.get_total(db_session, SHOPPER_ID, "magic_powder") == 1


def test_special_apple_sell_prices_are_unchanged():
    sells = CONFIG_DEFAULTS["item_sell_prices"]
    assert sells["apple_gold"] == 50
    assert sells["apple_diamond"] == 100
    assert sells["apple_obsidian"] == 200


def test_economy_placeholders_stay_unset():
    """The foundation's placeholder price keys must not become a second price.

    ``.get`` (rather than ``[]``) so the guard holds both before and after the
    marriage foundation config lands: absent or ``None`` both mean "no second
    source of truth", which is what we are protecting.

    ``flower_love_gain`` is deliberately NOT in this list: it is not a price,
    and the economy review deliberately gave it a real value.
    """
    for key in ("wedding_ring_buy_price", "flower_buy_price"):
        assert CONFIG_DEFAULTS.get(key) is None


def test_no_schema_or_domain_change_was_needed():
    """Scope guard: the items are plain ``UserItem`` rows, as before."""
    from sib.database.models import UserItem

    assert UserItem.__tablename__ == "user_items"
    # The domain reads the same keys the shop sells.
    from sib.services.love import FLOWER_ITEM_KEY
    from sib.services.marriage_proposal import WEDDING_RING_ITEM_KEY

    assert WEDDING_RING_ITEM_KEY == "wedding_ring"
    assert FLOWER_ITEM_KEY == "flower"
